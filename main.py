import sys
from astrbot.api.star import Star, Context, register
from astrbot.api import AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent
from .runtime import PlatformService
from .backend import Backend

@register('astrbot_plugin_heybox_reader','Wyj425','小黑盒独立搜索、内容读取、登录和共享缓存','1.0.2')
class PlatformReader(PlatformService, Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        Star.__init__(self,context)
        self.setup(context,config,'astrbot_plugin_heybox_reader','小黑盒',Backend)

    @filter.event_message_type(filter.EventMessageType.ALL, priority=sys.maxsize-10)
    async def observe(self,event: AstrMessageEvent):
        await self.watch(event)

    @filter.on_llm_request(priority=sys.maxsize-10)
    async def attach(self,event: AstrMessageEvent,req):
        await self.enrich(event,req)

    @filter.llm_tool(name='heybox_search')
    async def search_tool(self,event: AstrMessageEvent,query: str):
        """检索小黑盒公开内容，返回标题和链接，不代表已读取正文。

        Args:
            query(string): 要查找的主题关键词。
        """
        return await self.tool_search(event,query)

    @filter.llm_tool(name='heybox_read')
    async def read_tool(self,event: AstrMessageEvent,url: str):
        """读取小黑盒正文或字幕和部分评论。工具结果不含图片像素，不可据此猜图。

        Args:
            url(string): 原平台的内容链接。
        """
        return await self.tool_read(event,url)
