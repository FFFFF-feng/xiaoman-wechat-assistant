"""ClawBot 手机日程专用网关，不转发任何公众号回调或内部桥接接口。"""
from .local_gateway import create_gateway

app = create_gateway(18083, schedule_only=True)
