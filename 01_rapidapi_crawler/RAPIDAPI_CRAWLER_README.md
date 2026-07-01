# RapidAPI 卡片链接爬取脚本

主脚本：`rapidapi_search_cards_cdp.js`（Node 版，无需 pip 依赖）

默认输入：

```text
<PROJECT_ROOT>\host.json
```

默认输出：

```text
<PROJECT_ROOT>\rapidapi_card_links.json
```

## 推荐运行方式：Node 版本

这个版本不需要安装 pip/npm 依赖，会自动启动 Edge 或 Chrome，然后循环输入关键词并抓取卡片链接。

先进入脚本目录：

```powershell
cd <PROJECT_ROOT>\01_rapidapi_crawler
```

只测试前 3 个关键词：

```powershell
node rapidapi_search_cards_cdp.js --limit 3
```

正式运行：

```powershell
node rapidapi_search_cards_cdp.js
```

如果页面加载慢，可以增加等待时间，单位是毫秒：

```powershell
node rapidapi_search_cards_cdp.js --wait 8000
```

如果自动找不到浏览器，可以指定 Edge 或 Chrome 路径：

```powershell
node rapidapi_search_cards_cdp.js --browser "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
```

## 输出格式

输出 JSON 会长这样：

```json
[
  {
    "keyword": "aspose-pdf-cloud1",
    "links": [
      {
        "title": "example api title",
        "url": "https://rapidapi.com/example/api/example-api"
      }
    ],
    "error": null
  }
]
```

脚本每处理完一个关键词都会保存一次结果，中途失败也能保留已经爬到的部分。
