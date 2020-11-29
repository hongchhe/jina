import asyncio

from ..zmq import AsyncZmqlet
from ...logging import JinaLogger
from ...logging.profile import TimeContext
from ...proto import jina_pb2_grpc
from ...types.message import Message
from ...types.request import Request


class GRPCServicer(jina_pb2_grpc.JinaRPCServicer):

    def __init__(self, args):
        super().__init__()
        self.args = args
        self.name = args.name or self.__class__.__name__
        self.logger = JinaLogger(self.name, **vars(args))

    def handle(self, msg: 'Message') -> 'Request':
        msg.add_route(self.name, self.args.identity)
        return msg.response

    async def CallUnary(self, request, context):
        with AsyncZmqlet(self.args, logger=self.logger) as zmqlet:
            await zmqlet.send_message(Message(None, request, 'gateway',
                                              **vars(self.args)))
            return await zmqlet.recv_message(callback=self.handle)

    async def Call(self, request_iterator, context):
        with AsyncZmqlet(self.args, logger=self.logger) as zmqlet:
            # this restricts the gateway can not be the joiner to wait
            # as every request corresponds to one message, #send_message = #recv_message
            prefetch_task = []
            onrecv_task = []

            def prefetch_req(num_req, fetch_to):
                for _ in range(num_req):
                    try:
                        asyncio.create_task(
                            zmqlet.send_message(
                                Message(None, next(request_iterator), 'gateway',
                                        **vars(self.args))))
                        fetch_to.append(asyncio.create_task(zmqlet.recv_message(callback=self.handle)))
                    except StopIteration:
                        return True
                return False

            with TimeContext(f'prefetching {self.args.prefetch} requests', self.logger):
                self.logger.warning('if this takes too long, you may want to take smaller "--prefetch" or '
                                    'ask client to reduce "--batch-size"')
                is_req_empty = prefetch_req(self.args.prefetch, prefetch_task)
                if is_req_empty and not prefetch_task:
                    self.logger.error('receive an empty stream from the client! '
                                      'please check your client\'s input_fn, '
                                      'you can use "PyClient.check_input(input_fn())"')
                    return

            while not (zmqlet.msg_sent == zmqlet.msg_recv != 0 and is_req_empty):
                self.logger.info(f'send: {zmqlet.msg_sent} '
                                 f'recv: {zmqlet.msg_recv} '
                                 f'pending: {zmqlet.msg_sent - zmqlet.msg_recv}')
                onrecv_task.clear()
                for r in asyncio.as_completed(prefetch_task):
                    yield await r
                    is_req_empty = prefetch_req(self.args.prefetch_on_recv, onrecv_task)
                prefetch_task.clear()
                prefetch_task = [j for j in onrecv_task]
