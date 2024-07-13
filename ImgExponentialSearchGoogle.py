import asyncio
import hashlib
import logging
import os
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import List, Dict, Union

import aiohttp
import jieba
from bs4 import BeautifulSoup
from peewee import Model, CharField, SqliteDatabase
from rich import box
from rich.layout import Layout
from rich.panel import Panel

# 禁止 jieba 打印日志信息
jieba.setLogLevel(logging.ERROR)
from rich.live import Live
from rich.table import Table
from rich.console import Console
from tenacity import retry, stop_after_attempt, wait_fixed, retry_if_exception_type


@dataclass
class SimilarImageData:
    key_words: str
    data_item_title: str
    data_thumbnail_url: str
    data_action_url: str
    extension: str
    file_name: str


@dataclass
class ImageProcessResult:
    image_keywords: str
    similar_images: List[SimilarImageData]
    new_data_save_path: str
    existing_images: List[str]


# 图片指数搜索 Google 类
class ImgExponentialSearchGoogle:
    # 被排除的关键词
    shield_keywords_list = ['素材', '设计', '模板', '免费', '下载', '素材包', '模板集', '模板下载', '模板免费',
                            '模板分享', '网', '插画', '编号', '海报', '汇图', '壁纸', '背景', '图库', '图片', '像素',
                            '格式', '我', '图案']

    # 被确认的关键词
    allowed_keywords_list = ["华表", '敬礼', '飘带', '和平鸽', '纪念碑', '火炬']
    # 日志列表
    console_list = []
    # 上传地址
    url = "https://lens.google.com/v3/upload?hl=zh-CN&re=df&vpw=1683&vph=693&ep=gisbubb"
    headers = {
        'User-Agent': 'Apifox/1.0.0 (https://apifox.com)',
        'Accept': '*/*',
        'Host': 'lens.google.com',
        'Connection': 'keep-alive'
    }

    def __init__(
            self, input_dir="./input", output_dir="./output",
            allowed_keywords_list=None, shield_keywords_list=None, target_img_count=100,
            return_exceptions=True, database_path='SimilarImages.db', ):
        # 允许关键字列表
        self.allowed_keywords_list.extend(allowed_keywords_list if allowed_keywords_list else [])
        # 屏蔽关键字列表
        self.shield_keywords_list.extend(shield_keywords_list if shield_keywords_list else [])
        # 数据库
        self.db = SqliteDatabase(database_path)
        # 初始化数据库
        self._initialize_database()
        # 是否返回异常
        self.return_exceptions = return_exceptions
        # 目标图片目录
        self.input_dir = input_dir
        # 输出目录
        self.output_dir = output_dir
        # 目标图片数量
        self.target_img_count = target_img_count
        # 布局
        self.layout = None
        # 类似图片的关键字
        self.keyword_to_similar_images: Dict[str, List[SimilarImageData]] = {}
        # 输入图像列表
        self.input_image_list = self.list_files_by_extension(self.input_dir)
        # 允许关键字列表
        self.allowed_keywords_list.extend([Path(input_image_path).stem for input_image_path in self.input_image_list])
        self.console = Console()
        if not os.path.exists(self.output_dir):
            os.makedirs(self.output_dir)

    # 运行
    async def run(self):
        self.layout = Layout()
        self.layout.split_row(
            Layout(name="Table"),
            Layout(name="Log"),
        )
        with Live(self.layout):
            tasks = []
            for input_image_path in self.input_image_list:
                tasks.append(self.exponential_image_processing(input_image_path))
            await asyncio.gather(*tasks, return_exceptions=self.return_exceptions)

    # 指数图像处理
    async def exponential_image_processing(
            self, input_image_path: Union[str, Path], image_keywords: str = None, exponential: bool = True,
            target_img_count=None):
        target_img_count = target_img_count if target_img_count else self.target_img_count
        self.console_log(f"输入图像路径{input_image_path} 进行搜索")
        if not Path(input_image_path).exists():
            self.console_log(f"输入图像路径{input_image_path}不存在")
            return

        # 搜索相似图片获取图片处理结果
        image_process_result = await self.search_similar_images(
            image_keywords=image_keywords,
            input_image_path=input_image_path
        )

        # 没有找到相似图片
        if len(image_process_result.similar_images) == 0:
            self.console_log(f"没有找到相似图片{image_process_result.image_keywords, input_image_path}")
            return

        # 统计目录中的文件数量
        count_files_in_directory = self.count_files_in_directory(image_process_result.new_data_save_path)

        # 对数据进行切片避免过多处理
        image_process_result.similar_images = image_process_result.similar_images[
                                              :(target_img_count - len(image_process_result.existing_images))
                                              ]

        self.console_log(
            f"最新长度{image_process_result.new_data_save_path, count_files_in_directory} 文件取得数据{input_image_path, len(image_process_result.similar_images)}")

        # 更新表格
        if self.layout: self.layout["Table"].update(self.create_progress_table())

        # 更新关键词对应的搜索结果
        self.keyword_to_similar_images.setdefault(image_process_result.image_keywords, []).extend(
            image_process_result.similar_images)

        # 下载图片
        newly_owned_images = await self.download_images(image_process_result)
        # 插入数据
        self.insert_data(image_process_result)

        # 重新统计文件数量,判断是否继续遍历
        if self.count_files_in_directory(image_process_result.new_data_save_path) <= target_img_count and exponential:
            self.console_log(f"对新拥有的图片进行遍历{newly_owned_images}")
            exponential_image_processing_tasks = []
            for newly_owned_image_index, newly_owned_image_path in enumerate(newly_owned_images):
                if self.count_files_in_directory(image_process_result.new_data_save_path) >= target_img_count:
                    self.console_log(
                        f"长度足够{image_process_result.new_data_save_path, self.count_files_in_directory(image_process_result.new_data_save_path), self.keyword_to_similar_images[image_process_result.image_keywords]}")
                    if self.layout: self.layout["Table"].update(self.create_progress_table())
                    break
                await self.exponential_image_processing(
                    input_image_path=newly_owned_image_path,
                    image_keywords=image_process_result.image_keywords,
                    exponential=(newly_owned_image_index == len(newly_owned_images) - 1)
                )
                exponential_image_processing_tasks.append(self.exponential_image_processing(
                    input_image_path=newly_owned_image_path,
                    image_keywords=image_process_result.image_keywords,
                    exponential=(newly_owned_image_index == len(newly_owned_images) - 1)
                ))
            await asyncio.gather(*exponential_image_processing_tasks, return_exceptions=self.return_exceptions)

    # 下载图片
    async def download_images(self, image_process_result: ImageProcessResult):
        # 下载任务列表
        download_tasks = []
        # 下载图片保存路径列表
        download_image_save_path_list = []
        for similar_images in image_process_result.similar_images:
            # 下载图片保存位置
            download_image_save_path = os.path.join(image_process_result.new_data_save_path,
                                                    f"{similar_images.file_name}.jpg")
            download_image_save_path_list.append(download_image_save_path)
            download_tasks.append(self.download_image(similar_images.data_thumbnail_url, download_image_save_path))
        await asyncio.gather(*download_tasks, return_exceptions=self.return_exceptions)
        return download_image_save_path_list

    # 搜索相似图片
    async def search_similar_images(self, input_image_path, image_keywords=None) -> ImageProcessResult:
        if not Path(input_image_path).exists():
            self.console_log(f"输入图像路径{input_image_path}不存在")
            return ImageProcessResult(similar_images=[], image_keywords='', new_data_save_path='', existing_images=[])

        # 上传图片获取html源码
        html_source = await self.upload_image_get_html(input_image_path)

        # 将html源码转换为相似图片数据
        similar_images_data = self.parse_similar_images_from_html(html_source)

        # 提取关键词
        image_keywords = image_keywords if image_keywords else self.extract_keywords(''.join(
            [similar_images.data_item_title for similar_images in similar_images_data]))

        # 获取关键词下目录的文件
        new_data_save_path = os.path.join(self.output_dir, image_keywords)
        existing_images = [Path(path).stem for path in self.list_files_by_extension(new_data_save_path)]

        # 匹配关键字图像
        matching_keyword_images = [
            SimilarImageData(
                data_thumbnail_url=similar_images.data_thumbnail_url,
                data_item_title=similar_images.data_item_title,
                data_action_url=similar_images.data_action_url,
                extension=similar_images.extension,
                key_words=image_keywords,
                file_name=image_keywords + '_' + self.short_hash(similar_images.data_item_title)) for similar_images in
            similar_images_data if
            image_keywords in similar_images.data_item_title]

        unowned_images = [
            matching_keyword_image for matching_keyword_image in matching_keyword_images if
            matching_keyword_image.file_name not in existing_images]
        if len(unowned_images) == 0 and len(matching_keyword_images) > 0:
            unowned_images = [matching_keyword_images[-1]]
        return ImageProcessResult(
            image_keywords=image_keywords, similar_images=unowned_images,
            new_data_save_path=new_data_save_path, existing_images=existing_images
        )

    # 提取关键词
    def extract_keywords(self, text: str) -> str:
        words = jieba.lcut(text)
        # 有效关键词
        valid_words = [word for word in words if re.match(r'[\u4e00-\u9fa5a-zA-Z0-9]', word)]
        # 已过滤关键字
        filtered_keywords = [word for word in valid_words if word in self.allowed_keywords_list]
        if not filtered_keywords:
            # 对屏蔽词进行筛选
            filtered_keywords = [word for word in valid_words if word not in self.shield_keywords_list]
        keyword_counts = Counter(filtered_keywords)
        most_common_words = keyword_counts.most_common(1)
        allowed_keywords_list = [word for word, _ in most_common_words]
        return str('-'.join(allowed_keywords_list))

    # 上传图片获取 html
    @retry(stop=stop_after_attempt(5), wait=wait_fixed(5), retry=retry_if_exception_type(aiohttp.ClientError))
    async def upload_image_get_html(self, input_image_path):
        with open(input_image_path, 'rb') as f:
            form = aiohttp.FormData()
            form.add_field(
                'encoded_image',
                f,
                filename='图片.png',
                content_type='image/png'
            )

            async with aiohttp.ClientSession() as session:
                async with session.post(self.url, data=form) as response:
                    return await response.text()

    @retry(stop=stop_after_attempt(5), wait=wait_fixed(5), retry=retry_if_exception_type(aiohttp.ClientError))
    async def download_image(self, url, save_path):
        # 检查并创建目录
        session = aiohttp.ClientSession()
        directory = os.path.dirname(save_path)
        if not os.path.exists(directory):
            os.makedirs(directory)
        try:
            async with session.get(url) as response:
                with open(save_path, 'wb') as fd:
                    async for chunk in response.content.iter_chunked(1024):
                        fd.write(chunk)
                self.console_log(f"下载成功: {save_path}")
                if self.layout: self.layout["Table"].update(self.create_progress_table())
        except aiohttp.ClientError as e:
            self.console_log(f"下载失败:{save_path} {e}")
            raise
        finally:
            await session.close()

    # 图像数据库
    class ImageDatabase(Model):
        key_words = CharField()
        data_item_title = CharField()
        data_thumbnail_url = CharField()
        data_action_url = CharField()
        extension = CharField()
        file_name = CharField()

        class Meta:
            database = None  # 指定模型使用的数据库

    # 初始化数据库
    def _initialize_database(self):
        self.ImageDatabase._meta.database = self.db
        self.db.connect()
        self.db.create_tables([self.ImageDatabase])

    # 插入数据
    def insert_data(self, image_process_result: ImageProcessResult):
        for similar_image in image_process_result.similar_images:
            data_entry = self.ImageDatabase.create(
                key_words=image_process_result.image_keywords,
                data_item_title=similar_image.data_item_title,
                data_thumbnail_url=similar_image.data_thumbnail_url,
                data_action_url=similar_image.data_action_url,
                extension=similar_image.extension,
                file_name=similar_image.file_name
            )
            data_entry.save()

    # 创建进度表
    def create_progress_table(self):
        table = Table(show_header=True, border_style="dim", expand=True)
        table.add_column("关键词", style="magenta")
        table.add_column("数组长度", style="green")
        table.add_column("文件数量", style="green")
        for key, value in self.keyword_to_similar_images.items():
            new_data_save_path = os.path.join(self.output_dir, key)
            table.add_row(key, str(len(value)), str(self.count_files_in_directory(new_data_save_path)))
        for box_style in [
            box.SQUARE,
            box.MINIMAL,
            box.SIMPLE,
            box.SIMPLE_HEAD,
        ]:
            table.box = box_style
        return table

    # 日志
    def console_log(self, message, length=15):
        if self.layout:
            self.console_list.append(message)
            self.layout["Log"].update(Panel('\n'.join(self.console_list[-length:])))
        else:
            self.console.print(message)

    # 统计目录中的文件数量
    @staticmethod
    def count_files_in_directory(directory_path):
        try:
            path = Path(directory_path)
            return len([entry for entry in path.iterdir() if entry.is_file()])
        except FileNotFoundError:
            return 0
        except Exception:
            return 0

    # 按扩展名列出文件
    @staticmethod
    def list_files_by_extension(directory_path: Union[str, Path], extensions: List[str] = None) -> List[Path]:
        if extensions is None:
            extensions = ['.jpg', '.png']

        directory = Path(directory_path)
        if not directory.exists():
            # raise ValueError(f"The path '{directory}' does not exist.")
            return []

        # 检查是否是目录或文件并返回匹配的文件
        if directory.is_dir():
            return [file for file in directory.rglob('*') if file.suffix in extensions]
        elif directory.is_file() and directory.suffix in extensions:
            return [directory]
        else:
            raise ValueError(f"The path '{directory}' is neither a valid file nor a directory.")

    # 短哈希
    @staticmethod
    def short_hash(url):
        sha256_hash = hashlib.sha256(url.encode('utf-8')).hexdigest()
        return sha256_hash[:8]  # 取前8位作为文件名的一部分

    # 过滤符号
    @staticmethod
    def filter_symbols(text):
        pattern = r'[^a-zA-Z0-9\s]'
        filtered_text = re.sub(pattern, '', text)
        return filtered_text

    # 分析 html
    @staticmethod
    def parse_similar_images_from_html(html_source) -> List[SimilarImageData]:
        soup = BeautifulSoup(html_source, "lxml")
        similar_images = []

        video_autoplay_element = soup.find(attrs={"data-video-autoplay-mode": True})
        if not video_autoplay_element:
            return similar_images

        link_tags = video_autoplay_element.find_all('a')
        for link_tag in link_tags:
            next_sibling = link_tag.find_next_sibling()
            thumbnail_divs = link_tag.find_all('div', attrs={'data-thumbnail-url': True, 'class': 'OFiffe'})
            if next_sibling:
                for div in thumbnail_divs:
                    thumbnail_div = next_sibling.find('div', attrs={'data-thumbnail-url': True})
                    if not thumbnail_div:
                        continue
                    similar_images.append(SimilarImageData(
                        data_thumbnail_url=thumbnail_div.get('data-thumbnail-url'),
                        data_item_title=thumbnail_div.get('data-item-title'),
                        data_action_url=thumbnail_div.get('data-action-url'),
                        extension=div.get('data-action-url'),
                        file_name='',
                        key_words='',
                    ))
        return similar_images


async def main():
    # 初始化图片指数搜索 Google
    img_exponential_search_google = ImgExponentialSearchGoogle(
        input_dir="./input",  # 上传图片目录
        output_dir="./output",  # 输出目录
        allowed_keywords_list=['奶牛', '牛'],  # 允许的关键词列表
        shield_keywords_list=['素材', '模板'],  # 屏蔽关键词列表
        target_img_count=1000,  # 目标图片数量
        return_exceptions=False,  # 发生异常是否继续运行
        database_path='SimilarImages.db'  # 数据库路径
    )

    # 获取相似图片的数据
    Console().print("[bold red on yellow]获取相似图片的数据[/bold red on yellow]")
    search_similar_images = await img_exponential_search_google.search_similar_images(
        input_image_path='./input/奶牛_334e392b.jpg',  # 上传图片路径
        image_keywords='牛'  # 关键词
    )
    Console().print(search_similar_images)

    # 指定文件与关键词进行指数搜索下载
    Console().print("[bold red on yellow]指定文件与关键词进行指数搜索下载[/bold red on yellow]")
    await img_exponential_search_google.exponential_image_processing(
        input_image_path="./input/奶牛_334e392b.jpg",  # 上传图片路径
        image_keywords='奶牛',  # 关键词
        target_img_count=1000,  # 目标图片数量
        exponential=True  # 是否进行指数搜索
    )

    # 根据参数对上传图片进行指数搜索
    Console().print("[bold red on yellow]根据参数对上传图片进行指数搜索[/bold red on yellow]")
    await img_exponential_search_google.run()


if __name__ == '__main__':
    asyncio.run(main())
