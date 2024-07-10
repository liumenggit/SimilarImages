import asyncio
import hashlib
import os
from collections import Counter
from pathlib import Path
import aiohttp
import jieba
from bs4 import BeautifulSoup
from peewee import Model, CharField, SqliteDatabase

from tenacity import retry, stop_after_attempt, wait_fixed, retry_if_exception_type


class GoogleImgSearch:
    def __init__(self, image_dir="./upload", output_dir="./download", word_list=None, target_img_count=100):
        # 屏蔽
        self.shield = [
            ' ', '-', '_', ',', '素材', '设计', '模板', '免费', '下载', '素材包', '模板集', '模板下载', '模板免费',
            '模板分享', '网', '插画', '编号', '海报', '汇图', '壁纸', '背景', '图库', '图片', '素材', '模板',
            '素材包',
            '模板集', '模板下载', '模板免费', '模板分享', '网', '插画', '编号', '海报', '汇图', '壁纸',
            '背景', '图库',
            '图片', '素材', '模板', '素材包', '模板集', '模板下载', '模板免费', '模板分享', '网', '插画',
            '编号',
            '海报', '汇图', '壁纸', '背景', '图库', '图片', '素材', '模板', '素材包', '模板集', '模板下载',
            '模板免费',
            '模板分享', '网', '插画', '编号', '海报', '汇图', '壁纸', '背景', '图库', '图片', '素材', '模板',
            '素材包',
            '模板集', '模板下载', '模板免费', '模板分享', '网', '插画', '编号', '海报', '汇图', '壁纸',
            '背景', '图库', '像素', '格式', '我'
        ]
        #  # 默认关键词列表
        self.word_list = word_list if word_list else ["蔬菜", "华表", '敬礼', '飘带', '和平鸽', '纪念碑', '火炬']
        self.db = SqliteDatabase('SimilarImages.db')
        self._initialize_database()
        # 目标图片目录
        self.image_dir = image_dir
        # 输出目录
        self.output_dir = output_dir
        # 目标图片数量
        self.target_img_count = target_img_count

        self.url = "https://lens.google.com/v3/upload?hl=zh-CN&re=df&vpw=1683&vph=693&ep=gisbubb"
        self.headers = {
            'User-Agent': 'Apifox/1.0.0 (https://apifox.com)',
            'Accept': '*/*',
            'Host': 'lens.google.com',
            'Connection': 'keep-alive'
        }

        # self.session = aiohttp.ClientSession(headers=self.headers, connector=aiohttp.TCPConnector(ssl=False))

        self.img_key_words_dict = {}
        self.img_dir_list = self.get_file_paths(self.image_dir)
        self.word_list.extend([Path(image_dir).stem for image_dir in self.img_dir_list])

        if not os.path.exists(self.output_dir):
            os.makedirs(self.output_dir)

    class Data(Model):
        key_words = CharField()
        data_item_title = CharField()
        data_thumbnail_url = CharField()
        data_action_url = CharField()
        extension = CharField()
        file_name = CharField()

        class Meta:
            database = None  # 指定模型使用的数据库

    def _initialize_database(self):
        self.Data._meta.database = self.db
        self.db.connect()
        self.db.create_tables([self.Data])

    def insert_data(self, img_key_words, search_images_result):
        for result in search_images_result:
            data_entry = self.Data.create(
                key_words=img_key_words,
                data_item_title=result.get('data-item-title'),
                data_thumbnail_url=result.get('data-thumbnail-url'),
                data_action_url=result.get('data-action-url'),
                extension=result.get('extension'),
                file_name=result.get('file_name')
            )
            data_entry.save()

    async def loop_run(self):
        # 循环处理图片目录列表
        tasks = []
        for img_dir in self.img_dir_list:
            tasks.append(self.process_directory(img_dir))
        await asyncio.gather(*tasks)
        # await self.session.close()

    async def process_directory(self, img_dir, img_key_words=None, end=True):
        # print(f"对{img_dir}进行搜索")
        if not os.path.exists(img_dir):
            print(f"目录不存在{img_dir}")
            return

        # 将图像转为字典格式，包括关键词和搜索结果
        img_to_dict = await self.img_to_dict(img_key_words=img_key_words, img_dir=img_dir)
        img_save_path = os.path.join(self.output_dir, img_to_dict['img_key_words'])
        img_to_dict['search_images_result'] = img_to_dict['search_images_result'][
                                              :self.target_img_count - self.count_files_in_directory(img_save_path)]
        print(
            f"最新长度{img_save_path, self.count_files_in_directory(img_save_path)} 文件取得数据{img_dir, len(img_to_dict['search_images_result'])}")
        # 更新关键词对应的搜索结果
        self.img_key_words_dict.setdefault(img_to_dict['img_key_words'], []).extend(img_to_dict['search_images_result'])

        img_dir_list = await self.download_images(img_to_dict)

        self.insert_data(img_to_dict['img_key_words'], img_to_dict['search_images_result'])

        # 如果结果数量不足，继续搜索并遍历新目录
        if self.count_files_in_directory(img_save_path) <= self.target_img_count and end:
            # print(f"根据保存路径进行再次遍历{img_dir_list}")
            for index, sub_img_dir in enumerate(img_dir_list):
                if self.count_files_in_directory(img_save_path) >= self.target_img_count:
                    print(
                        f"长度足够{img_save_path, self.count_files_in_directory(img_save_path), self.img_key_words_dict[img_to_dict['img_key_words']]}")
                    break
                await self.process_directory(sub_img_dir, img_key_words=img_to_dict['img_key_words'],
                                             end=(index == len(img_dir_list) - 1))

    async def img_to_dict(self, img_key_words=None, img_dir=None):

        html_source = await self.upload_image_get_html(img_dir)
        # print(html_source)
        search_images_result = self.analyse_html(html_source)

        # 获取关键词
        img_key_words = img_key_words if img_key_words else self.get_key_words(''.join(
            [result['data-item-title'] for result in search_images_result]))

        # 过滤并更新搜索结果
        search_images_result = [
            {
                **result,
                'file_name': img_key_words + '_' + self.short_hash(result.get('data-thumbnail-url'))
            } for result in search_images_result if img_key_words in result.get('data-item-title', '')
        ]

        return {'img_key_words': img_key_words, 'search_images_result': search_images_result}

    @staticmethod
    def short_hash(url):
        sha256_hash = hashlib.sha256(url.encode('utf-8')).hexdigest()
        return sha256_hash[:8]  # 取前8位作为文件名的一部分

    def get_key_words(self, text):
        words = jieba.lcut(text)
        print(f"words{words}")
        filtered_words = [word for word in words if word in self.word_list]
        if not filtered_words:
            # 对屏蔽词进行筛选
            filtered_words = [word for word in words if word not in self.shield]
        word_counts = Counter(filtered_words)
        most_common_words = word_counts.most_common(1)
        word_list = [word for word, _ in most_common_words]
        return str('-'.join(word_list))

    @staticmethod
    def analyse_html(html_source):
        soup = BeautifulSoup(html_source, "lxml")
        results = []

        autoplay_elements = soup.find(attrs={"data-video-autoplay-mode": True})
        if not autoplay_elements:
            return results

        a_tags = autoplay_elements.find_all('a')
        for a_tag in a_tags:
            next_sibling = a_tag.find_next_sibling()
            OFiffe_divs = a_tag.findAll('div', attrs={'data-thumbnail-url': True, 'class': 'OFiffe'})
            if next_sibling:
                for div in OFiffe_divs:
                    thumbnail_div = next_sibling.find('div', attrs={'data-thumbnail-url': True})
                    if not thumbnail_div:
                        continue
                    results.append({
                        'data-item-title': thumbnail_div.get('data-item-title'),
                        'data-thumbnail-url': thumbnail_div.get('data-thumbnail-url'),
                        'data-action-url': thumbnail_div.get('data-action-url'),
                        'extension': div.get('data-action-url')
                    })
        return results

    @retry(stop=stop_after_attempt(5), wait=wait_fixed(5), retry=retry_if_exception_type(aiohttp.ClientError))
    async def upload_image_get_html(self, img_path):
        # files = [('encoded_image', (os.path.basename(img_path), open(img_path, 'rb'), 'image/png'))]
        text = ""
        with open(img_path, 'rb') as f:
            form = aiohttp.FormData()
            form.add_field(
                'encoded_image',
                f,
                filename='图层 1.png',
                content_type='image/png'
            )

            async with aiohttp.ClientSession() as session:
                async with session.post(self.url, data=form) as response:
                    return await response.text()

        # response = self.session.post(self.url, headers=self.headers, files=files)
        # return response.text

    @staticmethod
    def count_files_in_directory(directory_path):
        try:
            path = Path(directory_path)
            return len([entry for entry in path.iterdir() if entry.is_file()])
        except FileNotFoundError:
            return 0
        except Exception:
            return 0

    @staticmethod
    def get_file_paths(input_path, extensions=['.jpg', '.png']):
        input_path = Path(input_path)
        if not input_path.exists():
            raise ValueError(f"The path '{input_path}' does not exist.")

        if input_path.is_dir():
            return [p for p in input_path.rglob('*') if p.suffix in extensions]
        elif input_path.is_file() and input_path.suffix in extensions:
            return [input_path]
        else:
            raise ValueError(f"The path '{input_path}' is neither a file nor a directory.")

    async def download_images(self, img_to_dict):
        tasks = []
        save_path_list = []
        for img_info in img_to_dict['search_images_result']:
            filename = f"{img_info['file_name']}.jpg"
            save_path = os.path.join(self.output_dir, img_to_dict['img_key_words'], filename)
            save_path_list.append(save_path)
            tasks.append(self.download_image(img_info['data-thumbnail-url'], save_path))
        await asyncio.gather(*tasks)
        return save_path_list

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
                # print(f"下载成功: {save_path}")
        except aiohttp.ClientError as e:
            # print(f"下载失败:{save_path} {e}")
            raise
        finally:
            await session.close()


async def main():
    google_img_search = GoogleImgSearch()
    print(google_img_search.word_list)
    await google_img_search.loop_run()


if __name__ == '__main__':
    asyncio.run(main())
