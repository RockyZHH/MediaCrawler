import asyncio
import json
import httpx
import aiofiles
from datetime import datetime
import os
from typing import Callable, Dict, List, Optional
from urllib.parse import urlencode
from enum import Enum
from playwright.async_api import BrowserContext, Page

from tools import utils

from .exception import DataFetchError, IPBlockError
from .field import SearchNoteType, SearchSortType
from .help import get_search_id, sign


class NoteType(Enum):
    NORMAL = "normal"
    VIDEO = "video"


class XHSClient:
    def __init__(
            self,
            timeout=10,
            proxies=None,
            *,
            headers: Dict[str, str],
            playwright_page: Page,
            cookie_dict: Dict[str, str],
    ):
        self.proxies = proxies
        self.timeout = timeout
        self.headers = headers
        self._host = "https://edith.xiaohongshu.com"
        self.IP_ERROR_STR = "网络连接异常，请检查网络设置或重启试试"
        self.IP_ERROR_CODE = 300012
        self.NOTE_ABNORMAL_STR = "笔记状态异常，请稍后查看"
        self.NOTE_ABNORMAL_CODE = -510001
        self.playwright_page = playwright_page
        self.cookie_dict = cookie_dict

    async def _pre_headers(self, url: str, data=None) -> Dict:
        """
        请求头参数签名
        Args:
            url:
            data:

        Returns:

        """
        encrypt_params = await self.playwright_page.evaluate("([url, data]) => window._webmsxyw(url,data)", [url, data])
        local_storage = await self.playwright_page.evaluate("() => window.localStorage")
        signs = sign(
            a1=self.cookie_dict.get("a1", ""),
            b1=local_storage.get("b1", ""),
            x_s=encrypt_params.get("X-s", ""),
            x_t=str(encrypt_params.get("X-t", ""))
        )

        headers = {
            "X-S": signs["x-s"],
            "X-T": signs["x-t"],
            "x-S-Common": signs["x-s-common"],
            "X-B3-Traceid": signs["x-b3-traceid"]
        }
        self.headers.update(headers)
        return self.headers

    async def request(self, method, url, **kwargs) -> Dict:
        """
        封装httpx的公共请求方法，对请求响应做一些处理
        Args:
            method: 请求方法
            url: 请求的URL
            **kwargs: 其他请求参数，例如请求头、请求体等

        Returns:

        """
        async with httpx.AsyncClient(proxies=self.proxies) as client:
            response = await client.request(
                method, url, timeout=self.timeout,
                **kwargs
            )
        if not len(response.text):
            return response
        try:
            data = response.json()
        except json.decoder.JSONDecodeError:
            return response
        # data: Dict = response.json()
        if data["success"]:
            return data.get("data", data.get("success", {}))
        elif data["code"] == self.IP_ERROR_CODE:
            raise IPBlockError(self.IP_ERROR_STR)
        else:
            raise DataFetchError(data.get("msg", None))

    async def get(self, uri: str, params=None) -> Dict:
        """
        GET请求，对请求头签名
        Args:
            uri: 请求路由
            params: 请求参数

        Returns:

        """
        final_uri = uri
        if isinstance(params, dict):
            final_uri = (f"{uri}?"
                         f"{urlencode(params)}")
        headers = await self._pre_headers(final_uri)
        return await self.request(method="GET", url=f"{self._host}{final_uri}", headers=headers)

    async def post(self, uri: str, data: dict) -> Dict:
        """
        POST请求，对请求头签名
        Args:
            uri: 请求路由
            data: 请求体参数

        Returns:

        """
        headers = await self._pre_headers(uri, data)
        json_str = json.dumps(data, separators=(',', ':'), ensure_ascii=False)
        return await self.request(method="POST", url=f"{self._host}{uri}",
                                  data=json_str, headers=headers)

    async def pong(self) -> bool:
        """
        用于检查登录态是否失效了
        Returns:

        """
        """get a note to check if login state is ok"""
        utils.logger.info("[XHSClient.pong] Begin to pong xhs...")
        ping_flag = False
        try:
            note_card: Dict = await self.get_note_by_keyword(keyword="小红书")
            if note_card.get("items"):
                ping_flag = True
        except Exception as e:
            utils.logger.error(f"[XHSClient.pong] Ping xhs failed: {e}, and try to login again...")
            ping_flag = False
        return ping_flag

    async def update_cookies(self, browser_context: BrowserContext):
        """
        API客户端提供的更新cookies方法，一般情况下登录成功后会调用此方法
        Args:
            browser_context: 浏览器上下文对象

        Returns:

        """
        cookie_str, cookie_dict = utils.convert_cookies(await browser_context.cookies())
        self.headers["Cookie"] = cookie_str
        self.cookie_dict = cookie_dict

    async def get_self_info(self):
        uri = "/api/sns/web/v1/user/selfinfo"
        return await self.get(uri)

    async def get_note_by_keyword(
            self, keyword: str,
            page: int = 1, page_size: int = 20,
            sort: SearchSortType = SearchSortType.GENERAL,
            note_type: SearchNoteType = SearchNoteType.ALL
    ) -> Dict:
        """
        根据关键词搜索笔记
        Args:
            keyword: 关键词参数
            page: 分页第几页
            page_size: 分页数据长度
            sort: 搜索结果排序指定
            note_type: 搜索的笔记类型

        Returns:

        """
        uri = "/api/sns/web/v1/search/notes"
        data = {
            "keyword": keyword,
            "page": page,
            "page_size": page_size,
            "search_id": get_search_id(),
            "sort": sort.value,
            "note_type": note_type.value
        }
        return await self.post(uri, data)

    async def get_note_by_id(self, note_id: str) -> Dict:
        """
        获取笔记详情API
        Args:
            note_id:笔记ID

        Returns:

        """
        data = {"source_note_id": note_id}
        uri = "/api/sns/web/v1/feed"
        res = await self.post(uri, data)
        if res and res.get("items"):
            res_dict: Dict = res["items"][0]["note_card"]
            return res_dict
        utils.logger.error(f"[XHSClient.get_note_by_id] get note empty and res:{res}")
        return dict()

    async def get_note_comments(self, note_id: str, cursor: str = "") -> Dict:
        """
        获取一级评论的API
        Args:
            note_id: 笔记ID
            cursor: 分页游标

        Returns:

        """
        uri = "/api/sns/web/v2/comment/page"
        params = {
            "note_id": note_id,
            "cursor": cursor
        }
        return await self.get(uri, params)

    async def get_note_sub_comments(self, note_id: str, root_comment_id: str, num: int = 30, cursor: str = ""):
        """
        获取指定父评论下的子评论的API
        Args:
            note_id: 子评论的帖子ID
            root_comment_id: 根评论ID
            num: 分页数量
            cursor: 分页游标

        Returns:

        """
        uri = "/api/sns/web/v2/comment/sub/page"
        params = {
            "note_id": note_id,
            "root_comment_id": root_comment_id,
            "num": num,
            "cursor": cursor,
        }
        return await self.get(uri, params)

    async def get_note_all_comments(self, note_id: str, crawl_interval: float = 1.0,
                                    callback: Optional[Callable] = None) -> List[Dict]:
        """
        获取指定笔记下的所有一级评论，该方法会一直查找一个帖子下的所有评论信息
        Args:
            note_id: 笔记ID
            crawl_interval: 爬取一次笔记的延迟单位（秒）
            callback: 一次笔记爬取结束后

        Returns:

        """
        result = []
        comments_has_more = True
        comments_cursor = ""
        while comments_has_more:
            comments_res = await self.get_note_comments(note_id, comments_cursor)
            comments_has_more = comments_res.get("has_more", False)
            comments_cursor = comments_res.get("cursor", "")
            if "comments" not in comments_res:
                utils.logger.info(
                    f"[XHSClient.get_note_all_comments] No 'comments' key found in response: {comments_res}")
                break
            comments = comments_res["comments"]
            if callback:
                await callback(note_id, comments)
            await asyncio.sleep(crawl_interval)
            result.extend(comments)
        return result

    async def get_upload_files_permit(self, file_type: str, count: int = 1) -> tuple:
        """获取文件上传的 id

        :param file_type: 文件类型，["images", "video"]
        :param count: 文件数量
        :return:
        """
        uri = "/api/media/v1/upload/web/permit"
        params = {
            "biz_name": "spectrum",
            "scene": file_type,
            "file_count": count,
            "version": "1",
            "source": "web",
        }
        res = await self.get(uri, params)
        temp_permit = res["uploadTempPermits"][0]
        file_id = temp_permit["fileIds"][0]
        token = temp_permit["token"]
        return file_id, token

    async def upload_file(
            self,
            file_id: str,
            token: str,
            file_path: str,
            content_type: str = "image/jpeg",
    ):
        """ 将文件上传至指定文件 id 处

        :param file_id: 上传文件 id
        :param token: 上传授权验证 token
        :param file_path: 文件路径，暂只支持本地文件路径
        :param content_type:  【"video/mp4","image/jpeg","image/png"】
        :return:
        """
        # 5M 为一个 part
        max_file_size = 5 * 1024 * 1024
        url = "https://ros-upload.xiaohongshu.com/" + file_id
        if os.path.getsize(file_path) > max_file_size and content_type == "video/mp4":
            raise Exception("video too large, < 5M")
            # return self.upload_file_with_slice(file_id, token, file_path)
        else:
            headers = {"X-Cos-Security-Token": token, "Content-Type": content_type}
            # with open(file_path, "rb") as f:
            async with aiofiles.open(file_path, "rb") as f:
                return await self.request("PUT", url, data=f, headers=headers)

    async def create_note(self, title, desc, note_type, ats: list = None, topics: list = None,
                          image_info: dict = None,
                          video_info: dict = None,
                          post_time: str = None, is_private: bool = False):
        """创建日志"""

        if post_time:
            post_date_time = datetime.strptime(post_time, "%Y-%m-%d %H:%M:%S")
            post_time = round(int(post_date_time.timestamp()) * 1000)
        uri = "/web_api/sns/v2/note"
        business_binds = {
            "version": 1,
            "noteId": 0,
            "noteOrderBind": {},
            "notePostTiming": {
                "postTime": post_time
            },
            "noteCollectionBind": {
                "id": ""
            }
        }

        data = {
            "common": {
                "type": note_type,
                "title": title,
                "note_id": "",
                "desc": desc,
                "source": '{"type":"web","ids":"","extraInfo":"{\\"subType\\":\\"official\\"}"}',
                "business_binds": json.dumps(business_binds, separators=(",", ":")),
                "ats": ats,
                "hash_tag": topics,
                "post_loc": {},
                "privacy_info": {"op_type": 1, "type": int(is_private)},
            },
            "image_info": image_info,
            "video_info": video_info,
        }
        # headers = {
        #     "Referer": "https://creator.xiaohongshu.com/"
        # }
        print(data)
        return await self.post(uri, data)

    async def create_image_note(self, title, desc, files: list,
                                post_time: str = None,
                                ats: list = None,
                                topics: list = None,
                                is_private: bool = False,
                                ):
        """发布图文笔记

        :param title: 笔记标题
        :param desc: 笔记详情
        :param files: 文件路径列表，目前只支持本地路径
        :param post_time: 可选，发布时间，例如 "2023-10-11 12:11:11"
        :param ats: 可选，@用户信息
        :param topics: 可选，话题信息
        :param is_private: 可选，是否私密发布
        :return:
        """
        if ats is None:
            ats = []
        if topics is None:
            topics = []

        images = []
        for file in files:
            image_id, token = await self.get_upload_files_permit("image")
            await self.upload_file(image_id, token, file)
            images.append(
                {
                    "file_id": image_id,
                    "metadata": {"source": -1},
                    "stickers": {"version": 2, "floating": []},
                    "extra_info_json": '{"mimeType":"image/jpeg"}',
                }
            )
        return await self.create_note(title, desc, NoteType.NORMAL.value, ats=ats, topics=topics,
                                      image_info={"images": images}, is_private=is_private,
                                      post_time=post_time)
