```python
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
```