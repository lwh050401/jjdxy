# Lumen 精选商城（FastAPI + MySQL + LangGraph 客服「小马」）

中文电商前台 + 后台运营 + 智能客服。后端 FastAPI，数据全部落在 MySQL，前端页面用 Jinja2 服务端渲染，
客服由 LangGraph 编排，商品/订单/库存/推荐等电商接口封装成小马可调用的工具。

## 目录

```
电商智能体/
  app/
    main.py          应用工厂 create_app(surface)：同一份代码装出前台与后台两个进程；
                     API 路由先注册，静态资源挂载，SSR 兜底路由最后注册
    views.py         前台页面（/ /shop /product/{key} /cart /checkout /account /order/{no} /outfit /support）
    admin_views.py   后台页面（/admin，只在后台进程注册）
    admin_app.py     后台进程的入口（uvicorn app.admin_app:app）
    settings.py      .env 读取（pydantic-settings）
    db.py            SQLAlchemy + PyMySQL 封装，JSON/时间列的读写转换
    schema.sql       MySQL 建表脚本
    migrate.py       SQLite 导出数据 → MySQL 的迁移脚本
    agent/           小马：LangGraph 图、十个工具、会话与工具调用落库、离线路由
    seed_catalog.py  演示商品生成器（品名、卖点、规格组、实拍图匹配）
    api/             /api 下的 shop、auth、cart、wallet、agent、admin 六组接口
    services/        业务层：目录、购物车、结算、订单、库存、钱包、退款、图像指纹（visual）
  templates/ static/ 页面与样式、脚本、商品实拍图
  tests/             pytest 用例（打真库，跑完自动清理）
  data/sqlite-export.json  迁移脚本的输入，只含商品目录（账号、订单、会话一律为空表）
  data/uploads/      小马聊天里买家上传的图片，静态挂载在 /img/chat
```

## 启动

1. 启动本机 MySQL，建库 `lumen_store`（脚本会自动建表）。
2. 复制 `.env.example` 为 `.env`，填 `MYSQL_PASSWORD`、`SESSION_SECRET`（生产必须换掉默认值）。
   `.env` 含密码，不要提交、不要贴进聊天记录。
3. 装依赖、迁数据、起服务：

```bash
pip install -r requirements.txt
python -m app.migrate            # 首次或想重灌时加 --force
python -X utf8 -m app.services.visual --build   # 给全站商品实拍图建指纹，按图找相似要用（表为空时也会自动构建）
python -m uvicorn app.main:app --host 127.0.0.1 --port 3100        # 前台（商城 + 小马）
python -m uvicorn app.admin_app:app --host 127.0.0.1 --port 3101   # 后台（运营面板）
```

打开 http://127.0.0.1:3100 ，接口文档在 http://127.0.0.1:3100/api/docs ；后台在 http://127.0.0.1:3101/admin 。

## 仓库里没有任何账号

这个仓库发布的是代码：不含 `.env`，不含任何真实或演示的用户、管理员、买家账号，
`data/sqlite-export.json` 里 `users` / `sessions` / `addresses` / `orders` / `payments` /
`reviews` / `support_messages` / `audit_logs` / `analytics_events` 这些表全是空数组，
只有商品目录（分类、商品、图片、规格、库存流水、运费方案、优惠券、FAQ、站点设置）带着数据。

迁完数据后自己造第一个账号与管理员：

1. 打开 http://127.0.0.1:3100/account 注册一个买家账号（密码要求 8 位以上、含字母和数字）。
2. 把它提成管理员：`UPDATE users SET role='admin' WHERE email='你注册的邮箱';`
   改完在前台重新登录就会自动跳后台 3101；也可以直接在 http://127.0.0.1:3101/admin/login 登。
3. 买家侧的分支（余额不足、超时未付）用新注册账号自己下单即可，不要往仓库里提交任何账号数据。

`tests/conftest.py` 的 `admin_account` 夹具走的是同一条路：注册一次性邮箱，再把 `role` 改成 `admin`，
跑完连会话、地址、订单一起删干净，所以 `tests/` 不依赖任何预置账号。

> 本地开发时的浏览器探针脚本（Playwright 打真服务、截图那批 `.run/check_online*.py`、
> `check_visual.py`、`check_outfit.py`、`check_chat_cards.py`、`check_restock.py`、`check_expiry.py` 等）
> 依赖运行中的服务和演示数据，没有随仓库发布；下文提到它们的地方是在说明当初怎么验收，
> 仓库内可复现的验证是 `tests/` 那套 pytest 用例。

## 前台与后台分端口

同一份代码用 `create_app(surface)` 装出两个进程，`surface` 决定挂哪一面路由：

- 3100 前台：`/`、`/shop`、`/product/{key}`、`/cart`、`/checkout`、`/account`、`/order/{no}`、`/outfit`、`/support`
  与 `/api` 下的 shop、auth、cart、wallet、agent 五组接口。
- 3101 后台：`/admin` 面板、`/admin/login` 登录页，与 `/api/admin/*`（22 个接口方法，其中 `login` / `logout` 不要会话）。

拆开的意义是不让买家会话摸到运营接口：前台进程里 `/admin` 与 `/api/admin/*` 都是 404（路由根本不存在，
不是靠鉴权挡住），后台进程里 `/shop`、`/api/products` 同样 404。`tests/test_pages.py::test_surfaces_do_not_overlap`
钉住这条边界。

三件事跟着端口走，改动时别漏：

- 后台有自己的一套登录入口：`/admin/login` 页面 + `POST /api/admin/login` / `/logout`（`app/api/admin.py` 里的
  `public_router`，与那组默认过 `require_admin` 的接口分开注册，否则永远登不进去）。这个表单只认 `role=admin`
  的账号：买家账号即使密码正确也返回 403，并且刚校验密码时开出来的那条会话会当场注销，不在后台端口上留活会话。
  未登录打开 `/admin` 会 302 到 `/admin/login`；带着买家会话打开则带 `?reason=not_admin`，页面上说清楚是身份不对。
  前台 `/api/login` 签发的还是同一个 `lumen_sid`，浏览器 cookie 不分端口，所以在前台登录过管理员再开 3101 仍然直接进得去；
  两条路径的用例都在 `tests/test_pages.py`。换域名部署时要配 `SHOP_BASE_URL` / `ADMIN_BASE_URL`，
  模板里跨面的链接都取这两个绝对地址（`base.html` 顶部注入 `window.LUMEN_URLS`）。
- 静态资源两边都挂：后台要看商品缩略图，小马会话详情要点开买家传的图（`/img/products`、`/img/chat`）。
- 超时订单的扫描循环只跑在前台进程（`create_app` 里 `surface != "shop"` 直接返回），两个进程都扫会互相抢单。

后台侧的兜底 404 在 `app/admin_views.py`：`/api/` 前缀返回 JSON，其余渲染 `not_found.html`，
返回按钮指向前台的 `/shop`。

## 小马（智能客服）

- 入口：右下角浮窗、独立页 `/support`、接口 `POST /api/agent/messages`。
- 浮窗可拖动（按住标题栏，双击标题栏=放大/还原）、可拉伸（右下角把手）、可放大到整个视口再还原；
  位置与尺寸存 `localStorage` 的 `lumen_xiaoma_box`，刷新和换页面后会回到上次放的地方，并始终被夹在视口内。
- 大脑两条路径，`DEEPSEEK_API_KEY` 有值就走 LangGraph + DeepSeek（OpenAI 兼容协议），
  留空或调用失败自动走站内意图路由 `offline-router`，页面会标注「站内意图路由」，答案同样来自真实查库。
  接口返回里的 `model` 说的是这一轮真正作答的那一个，降级时不会写着 `deepseek-chat` 却其实是兜底。
- LangGraph 的 `messages` 通道必须带 `add_messages` 累加器：没有它，`ToolNode` 写回的工具消息会整表覆盖通道，
  把带 `tool_calls` 的那条 AI 消息挤掉，DeepSeek 会以 400「tool 消息没有对应的 tool_calls」拒收，于是每轮都退回离线。
- 在线一跳发出去的系统提示就两份：`SYSTEM_PROMPT`（738 字）+ 工具 schema。以前每次调用还会在尾部补一条
  「可用工具：…」的 system 消息（166 字，一轮最多 4 跳），`bind_tools` 早就把名称、说明和参数 schema 全发了，
  那条纯属重复付费，已删；系统提示里原本重抄一遍的参数口径（体重不是身高、预算是单件上限、穿搭位顺序、
  不说「同款」）也挪回各工具的 docstring，只在一处维护 —— 系统提示从 1122 字降到 743 字。
  `tests/test_agent.py` 有两条守卫：提示词字数上限，以及「这三条口径必须出现在工具说明里」。
- 同一轮里 `(工具名, 参数)` 完全相同的第二次调用直接复用上一份结果，返回时补一句
  「本轮已经用完全相同的条件查过一次」，并且不再写 `agent_tool_calls`、不再重复收商品卡
  （缓存挂在 `tools.turn_cache` 这个 ContextVar 上，`reset_turn()` 每轮清空，跨请求互不影响）。
  复用的是结果不是消息：两次 `tool_call_id` 都各自回一条 ToolMessage，消息序列依旧完整。
- 离线图编译一次就复用（`build_offline_graph()` 里的模块级单例）。Key 失效或 DeepSeek 报错时每轮都降级到它，
  以前每轮重新 compile 一遍，白烧一次构图。
- 模型自己看不出对方登没登录，所以 `ask()` 把 `logged_in` 带进状态，`_messages()` 按登录态在系统提示尾部接一条
  `LOGIN_NOTE` / `GUEST_NOTE`：登录了就明说「问『我的订单』直接调 `query_order`、`order_no` 留空，别反过来要订单号」，
  游客就明说「先要订单号或让对方登录，不许猜」。这条是补的洞 —— 没有它，测试买家登录后问「我的订单最近怎么样了」，
  在线大脑 3 次里有 3 次没查库、只顾跟人要订单号；补上之后 3/3 直接返回该账号里最近的单，游客那 2 次仍然只引导、不查别人的单。
  说明拼在同一条 system 消息里，不额外开一条，也不进主提示（主提示仍是 738 字）。
- 十个工具都直接调 `app/services` 里的同一份业务函数，和前台用的是一个口径：
  商品检索、商品详情、订单查询、库存查询、商品推荐、场合穿搭、配送方案、可用优惠码、常见问题、按图找相似。
- 每轮对话写 `agent_threads` / `agent_messages` / `agent_tool_calls`，后台「小马会话」页能按会话看回消息与工具调用，
  工具名展示为中文，英文 code 作为副标题保留。查到的条数由 `tools.result_count()` 统一算，
  在线与离线两条路径共用，前台标签按工具配量词（订单是「笔」、商品是「件」），不会出现「查到了却显示 0 条」。
- 订单号形如 `LM202610ABCDEF`（LM + 6 位数字 + 6 位字符），买家问到订单号会直接精确查询；
  别人的订单查不到，只提示用下单账号登录后再问。
- 回答里的订单号在前端会自动变成链接，点开就是 `/order/{订单号}` 详情页，物流与售后进度都在那一页。
  详情页与 `/api/orders/{订单号}` 用同一套归属校验：非管理员只看自己的单，游客拿别人的单号只能拿到 404。
- 小马查出的商品一律以卡片呈现，点卡片进 `/product/{slug}`。什么行算卡片只写在 `tools.card_rows()` 一处，
  在线（`_collect` / `_log`）与离线（`offline.run`）共用，换个大脑不能少几种商品：只认带 `slug` 或 `url` 的行，
  `recommend.*` 那些只有 slug 的由 `tools.with_link()` 补成链接；订单行、规格行不是商品，不会混进橱窗。
  库存问答给 1 张，检索/推荐/穿搭/按图找相似最多 6 张（`tools.CARD_LIMIT`）。
- 卡片刷新后还要在。落库的 `tool_calls[*].preview` 存的是卡片对象（只留 `tools.CARD_KEYS` 那几个字段，
  不把 description、variants 整包塞进 JSON 列），`/api/agent/history` 按消息逐条回放 `tool_calls` 与 `cards`，
  前端 `initChat()` 开局用本地存的 `lumen_xiaoma_thread` 拉一次历史重画对话。
  以前在线那条路径的 preview 只存了截到 4 条的**标题字符串**，`store.cards()` 却拿它当卡片用 —— 一刷新卡片就变成
  点不开的空卡，且在线比离线少一批；现在 `store.cards_from_calls()` 只认带 url 的字典，库里更早那批字符串行直接跳过，
  宁可不显示也不拼出 `/product/undefined`。
- 穿搭卡在商品图左下角标出穿搭位（主件/内搭/鞋履…，`.chat-role`），和回复文字里的顺序对得上；
  按图找相似的卡在右上角标相似度（`.chat-similar`），两个角标位置错开、能共存。
- 在线大脑爱用 markdown 写「**单件价格上限**」这类强调，前端 `answerHtml()` 先转成 `<b>` 再换行，
  气泡里不会留下成对的星号。

## 场合穿搭（`/outfit` 与小马的第 9 个工具）

- 页面在导航「场合穿搭」，条件只有七个：场合、着装线（男装/女装/不限）、体型、身高、风格、自己说的偏好、单件价格上限。
  下拉框里的取值全部来自 `outfit.vocabulary()`，只列站内真有现货的款数，选了就一定凑得出东西。
- 判断依据是商品 `attributes` 里那几个穿搭字段（`适用场合`、`穿搭位`、`风格`、`适合体型`、`适合身高`、`正式度`、
  `季节`、`颜色系`、`着装线`），由 `app/seed_catalog.py` 的 `OUTFIT` 词表写入，引擎不临场编参数。
- 凑一套的顺序是先定主件，再按主件自带的 `搭配位` 补外套/内搭/下装/鞋履/包袋/配饰；配不齐的位子在页面上写「还差 X」，
  只剩一件包或一只鞋时宁可不给（兜底组合要求 ≥2 件且含外套或内搭）。两套之间按「品类」去重，鞋包先避开上一套用过的。
- 体型与身高只淘汰穿在身上的单品（`WEAR_ROLES` = 主件/外套/内搭/下装），包袋、配饰、鞋履只加分不排除，
  否则一套里永远配不出包。风格词同理只对穿在身上的单品硬筛：手拿包标的是「正式、个性」，
  用户说温柔风也要配得出包，只是不加分。季节只参与排序，不当季的不会被删掉——页面上的说明就是照这个写的。
- 预算是「单件价格上限」，不是整套上限，`reason` 与页面都这么写；给了预算就在合格品里偏向便宜的（最多 8 分，
  与评分同量级，压不过版型与正式度），否则 1000 元预算会顶格配出 970 的西装加 981 的鞋。
  合计仍可能超过预算，那一套的说明里会写清「这个数是按每件单品筛的，不是整套上限」。
- 身高只认厘米：`_height_of` 先把「体重160」「160斤」「62kg」「预算1500」这类数字抹掉再找身高，
  「身高185体重160」不会按 160cm 筛衣服；小马的在线提示词也写了同一条，只报体重时不替用户估体型。
- 用户嘴里的场景比商品标签杂：`OCCASION_SYNONYMS` 把「相亲」「露营」「出差」「当伴娘」这类说法映射到站内真有货的场合，
  回复第一句会写「『相亲』站内按『约会』来配」，不假装站内有那个场合的专属货。性格词（内向、干练…）只按
  `PERSONALITY_STYLES` 映射成风格词，页面和小马都不判断长相气质。
- 商品详情页的介绍下方有「按「婚礼」凑一套搭配」，带着该商品的场合与着装线跳回 `/outfit`。

## 按图找相似（小马的第 10 个工具）

- 聊天窗（浮窗与 `/support`）传图有三个入口：点输入框左边的「上传图片」图标、把图片拖进对话区、
  或者截图后在对话框里 Ctrl+V 粘贴。粘贴只认剪贴板里的图片，纯文字照常粘贴不被拦；
  剪贴板里的截图通常没有文件名，待发送区顶上「粘贴的截图.png」。
  支持 PNG / JPEG / WebP，单张 ≤5 MB（`CHAT_UPLOAD_MAX_BYTES`），一次发一张；只传图不打字也行，
  默认按「这张图在站内有相似的吗」来问。
- 站内用的 DeepSeek 是纯文本模型，所以相似度是本地算的：`app/services/visual.py` 用 Pillow 把图缩到 32×32，
  先按外圈 2 像素的中位数认出棚拍白背景并把背景像素剔掉，再取 HSV 色相直方图（12 格）、明度直方图（5 格）、
  前景平均色、彩色像素占比、明暗对比、边缘密度（FIND_EDGES）和 64 位 dHash。
  距离 = 0.32×配色 + 0.20×明调 + 0.16×前景色 + 0.20×轮廓（1−汉明距）+ 0.12×细节。
- 只比颜色和轮廓，**认不出款式、品牌、材质**，因此回复与工具说明都不写「找到了同款」；
  相似度低于 `WEAK_MATCH`（0.70）时回答里会明说「这个相似度只算『接近』，不算同款」。
- 商品侧的指纹存在 `product_image_signatures`（按图片 URL 去重，一张图一条），首次调用或表为空时自动构建；
  手动重建用 `python -X utf8 -m app.services.visual --build`，加 `--force` 全量重算。
  上传的图片落 `data/uploads/`（挂载在 `/img/chat`），元数据与指纹写 `agent_images`，
  `token` 形如 `img_` + 16 位十六进制，会话建立后回填 `thread_id`，随会话级联删除。
- 接口：`POST /api/agent/images`（multipart，字段 `file`，可带 `thread_id`）返回
  `token / url / color / width / height / bytes`；`POST /api/agent/messages` 多接一个 `image_token`；
  `GET /api/agent/state` 里回 `upload: {accept, max_bytes}`，前端的大小与类型校验读的就是这一份。
- 离线路由里图片分支排在订单号识别之前：带了 `image_token` 就必定走 `find_similar_by_image`，
  品类和预算从同一句话里读（`_category_slug` / `budget_of`），命中商品最多每张图 2 件、总 5 件，
  卡片左上角标「相似 N%」（比的是图片，不是「同款识别」）。
- 后台「小马会话」的详情里带 `images`：`GET /api/admin/agent/threads/{id}` 会把这段会话传过的图列出来，
  页面显示缩略图与「文件名 宽×高」，点开就是 `/img/chat/...` 原图。
- 相似度是图片级别的：站内一张实拍图常挂着 10+ 件商品（实测最多 251 件共用一张），所以它们会拿到同一个分数，
  传站内的图自然 100%。这是按图比色的设计，不是识别出了同一件货。
- 传图频控单独收紧：每分钟 6 次（`_UPLOAD_LIMIT`），因为每张图都要现算描述子，和提问的 20 次/分钟不是一个量级。

## 数据口径

- 金额一律以「分」存库（`*_cents`），展示层再转元。
- 可售 = `variants.stock_qty - variants.reserved_qty`。下单时 `reserved_qty` 累加并写一条
  `reservations` 明细；支付成功扣真实库存并把明细置为 `consumed`，取消或超时释放占用并把明细置为 `released`。
  `reservations` 只是明细账，读数时不能再减一次。
  超时那条靠 `app/main.py` 的定时扫描循环（只在 `surface == "shop"` 的那个进程里启动，两边都扫会互相抢单）：启动先清一遍，之后每隔 `ORDER_SWEEP_SECONDS`（默认 60 秒）
  调一次 `checkout.release_expired()`，把 `pending_payment` 且 `expires_at` 已过的单置为 `expired`、
  放回占用、回退优惠券用量并写一条 `expired` 事件。只在启动时跑一次等于没有——服务连续开着的时候，
  过期单会一直挂着占库存，所以这条链由 `.run/check_expiry.py` 对着真服务核对。
- 退货退款（`kind='return'`）同意时回补库存；不勾选明细的整单退货按全部明细回补。仅退款不回补。
- 表名注意：规格表是 `variants`（不是 `product_variants`），`users` 主键是 `id`。

## 演示商品数据

`app/migrate.py` 只搬来 20 件原始商品，分类侧栏每类只有一两件。要把目录灌满用：

```bash
python -m app.seed_catalog --count 2000     # 按现有分类均分补到 2000 件（只用于空库首次灌数据）
python -m app.seed_catalog --purge          # 只删演示商品
python -m app.seed_catalog --count 2000 --force   # 先 purge 再重灌
python -m app.seed_catalog --fix-copy       # 只修介绍文案：补亮点、删掉误标成「材质」的关键词
python -m app.seed_catalog --outfit           # 建「场合装」分类、补齐场合装现货、给全站商品补穿搭属性
python -m app.seed_catalog --occasion-more 16 # 把每个场合装品名凑到 16 件：只往后加，一条都不删
python -m app.seed_catalog --occasion-rebuild # 改价位带后重灌场合装（被订单/购物车引用过就自动跳过）
python -m app.seed_catalog --fill 4          # 全站增量补货：把每个品名凑到 4 件，只加不删
python -m app.seed_catalog --fill 4 --only toy,garden   # 只补这几个分类
```

- `--fill` 是给「已经跑过一段、库里有了真订单」的站补货用的，和 `--force` 正好相反：它先按
  `attributes.品类` 数出每个品名现有几件，只把缺口补齐，index 接着现有最大编号往后排，全程只 INSERT。
  新品类第一次灌也走这条命令（`ensure_categories()` 会先把缺失的分类建出来，父分类排在第二轮建）。
  `--force` 会先 `--purge` 删掉全部 `seed-` 商品，被订单引用的那些一起没，任何时候都别在有真单的库上跑。

- 演示商品的 slug 统一以 `seed-` 开头，`--purge` 就按这个前缀整体回滚，原始 20 件商品不受影响。
- 品名按「便宜的在前、贵的在后」排，价格按品名位置在分类区间里取几何分布，所以同类里从低到高都有，
  排序和价格筛选能看出差别。
- 图片只按**品名**分配：`app/seed_data/name_images.json` 一行「品名 → 一张实物图」，一张图只挂它真的画着的那个品名。
  以前这里有一条分类兜底（`CATEGORY_FILES`，已删），结果是毛巾套装挂着空气净化器的照片、台球杆挂着篮球的照片——
  图比价格更容易骗人，所以宁可无图，不可错图。`image_pool()` 现在只认清单，配不到就返回空，
  前台 `.no-photo` 显示浅灰斜纹 + 线性图标 + 「暂无实物图」（商品卡、详情页大图、聊天卡片三处同一套口径；
  购物车、订单、穿搭页那些小缩略位仍用 `placeholder.svg`）。`catalog.serialize()` 不再往 `image` 里塞通用占位图，
  首页的三个大图位（首屏轮播、本周精选、新品）只从**确实有实物图**的商品里挑。
- 补图是一条可续跑的流水线，一次跑不完没关系，缺的会一直在缺口表里等着：

  ```bash
  python -X utf8 .run/build_name_images.py            # 品名总数 / 已有同物图 / 待补图，写 name_images.json 与 name_image_todo.json
  python -X utf8 .run/name_image_batch.py --count 40  # 打印下一批要画什么（目标文件名 + 提示词）
  python -X utf8 .run/name_image_batch.py --adopt <生成图所在目录>   # 收图：按目标名落进 static/img/products，顺手压到 800x1000
  python -X utf8 -m app.seed_catalog --rebind-images  # 按清单重挂全站演示商品的图，不动商品、规格与价格
  python -X utf8 -m app.services.visual --build       # 新图算指纹，按图找相似才认这批
  python -X utf8 .run/check_images.py                 # 错版门禁：多图 / 图文件缺失 / 挂错品名必须全为 0
  ```

  目标文件名是「分类 slug + 品名在该分类里的序号」（`digital-001.png`），序号只增不改，所以清单不会串位；
  `--adopt` 认不出名字的一律忽略，跑过一遍就自动跳过已有图的品名，中断后重跑同一批不会画歪。
  补图之后要跑一次 `python -m app.services.visual` 重建图片签名，按图找相似才认这批新图。
- 每件商品在 `CATALOG[slug].facts` 里写死三条卖点，详情页据此生成「商品介绍」：导语存 `products.description`，
  三条卖点存 `products.attributes` 的 `亮点`（参数表会跳过这个列表键），「配送与售后」那一栏读的是站内真实的
  运费方案与包邮门槛，不是写死的文案。卖点按品名配，不会给手机壳承诺 65W 快充。
  从旧 Node 商城迁来的 20 件原始商品用 `--fix-copy` 从自家导语里拆句补齐：导语拆得出三句的补齐三条，
  只拆得出两句的就留两句（Glow 精华面霜、Pulse 智能跳绳没有第三句出处，不替它们编），「可机洗」这种三字短句
  也算卖点、不按长度丢句。全站 6480 件在售商品都有 2~3 条亮点，口径由 `test_intro_points_come_from_their_own_copy` 守着。
  这条命令还会删掉旧库把关键词误标成「材质」的那一行参数（充电器写着「材质：快充」），关键词本来就在标签里，不会丢信息。
- 场合装是第 17 个分类（`seed-occasion-*`，25 个品名 × 12 件 = 300 件）：男装从正装衬衫、两粒扣/三粒扣西装、马甲、
  西裤到乐福鞋、公文包、口袋巾，女装从小礼服裙、内搭吊带、链条小包到玛丽珍与粗花呢短外套，另有中性羊毛大衣，
  凑一套要用的每个搭配位都有对应品名。`--outfit` 会把这套字段回填给全站商品（穿搭位/适用场合/风格/适合体型/适合身高/
  正式度/季节/颜色系/着装线）。品名各自带价位带（`OCCASION_PRICE`）并按品牌档次拉开：整类共用一个 199–2599 会灌出
  「领带比西装贵十倍」这种一看就是编的价；男装衬衫按领围、西裤按腰围给规格，不出现「领带选 2XL」。
  版型建议还反向补体型标签：`适合身高` 下限 ≤155 自动算小个子、上限 ≥182 算高挑，避免出现「建议身高 150-175 却不标小个子」这种自相矛盾的参数。
  西装类与皮鞋、衬衫的身高区间都写到 190cm，185cm 的用户才不会被告知「站内没有合适的内搭」。
- 加了新品名后用 `--occasion-more`（或 `--outfit`）补齐：按「每个品名各 12 件」算缺口，从件数最少的品名开始补，
  index 接着现有最大编号往后排，只 INSERT 不 DELETE，被订单引用过的场合装一条都不会动。
- 规格组按品名匹配（`OPTION_RULES`）：鞋类给鞋码，服饰给 S–2XL，包袋配件给颜色或容量，
  不再出现「T 恤选 42 码」。小马的 `product_detail` 工具读的就是这同一批字段。
  新品类的规格口径写在自己数据包的 `by_name` 里（同一类里锅看口径、刀具看件数），优先级高于 `OPTION_RULES`
  那种按子串猜的规则；往 `fashion` 这类老分类补「登山鞋」这种品名时，用 `OPTION_RULES_EXTRA` 追加规则，
  追加的排在已有规则之后，不会把「板鞋」的鞋码抢走。
- 品类词表不在 `seed_catalog.py` 里堆着，拆在 `app/seed_data/` 下按模块分文件（一个模块管几类商品），
  导入时由 `merge_seed_data()` 合并。四张表的字段契约写在 `app/seed_data/__init__.py` 的文档串里。
  撞车一律报错不静默覆盖：同一个 slug 顶掉站内已有分类、同一个品名挂在两个分类下，都会在导入时直接停下 ——
  静默赢的一方会把另一方的商品吞掉。单个模块写坏也只记进 `LOAD_ERRORS`，不连累并行中其他模块的自验。
  `merge_seed_data()` 的调用点在 `OPTION_RULES` 定义之后（不是文件上半部分）：它要把各模块的
  `OPTION_RULES_EXTRA` 追加进那张表，挪回上半部分会在导入时就 `NameError`。
- SKU 中间的分类段由 `sku_code(slug)` 生成：slug 按下划线切开、每段取前三个字母再截五位，
  所以 `baby_food`→`BABFO`、`baby_gear`→`BABGE`。老写法只取 slug 前三字母，这两个类都是 `BAB`，
  而 `variants.sku` 是全局唯一键——一次灌库两条撞在一起，整批 INSERT 被唯一键挡下来，
  前一类的商品已经提交，站就成了一半新一半旧。单词 slug 仍是前三个字母，站内老商品的码不变。
- 补货一共跑了三轮，规模从 16 个分类 2000 件一路加到 79 个分类、6480 件在售商品、1040 个品名（每个品名至少 4 件）。
  第二轮往外扩的是长尾品类与组合问法（鱼竿、电竞椅、颈椎牵引器、烘焙模具、扫地机耗材这 143 个词），
  第三轮补的是需求说法（待产包、送妈妈、猫咪用品、开学文具这 28 个词）。
- 商品图按品名重挂之后，站内 1234 个品名里 117 个有同物图（覆盖 1283 件演示商品），其余 1117 个还在缺口表里排队补，
  没补到的前台一律显示「暂无实物图」，不再借用别的商品的图。

## 购物需求覆盖度与搜索同义词

「连查五次都是 0 件」是这轮补货的起点，所以补没补够不能靠感觉，得逐个词查：

```bash
python -X utf8 .run/check_coverage.py --dump-gaps   # 1022 个日常说法逐个查，缺口写给施工图
python -X utf8 .run/plan_seed_fill.py               # 校验每个缺口词只有一个归属，产出 .run/seed_spec.json
python -X utf8 .run/lint_seed_data.py               # 数据包自验：字段、词表接住没、文案踩线没
python -X utf8 .run/lint_seed_data.py grocery       # 只查一个模块
```

- `.run/check_coverage.py` 的词表按购物场景分组（58 组、去重后 1022 个说法），判的是 `catalog.search()` 的真实命中数，
  所以这里绿的词，前台搜索框和小马的 `search_products` 两处都能查到货。有缺口退出码 1。
  光「查得到一件」不算过：命中少于 3 件的词会单列出来提示挑得太薄（用户点进去就没得比），
  补法是往对应分类的数据包里再加品名，然后 `--fill 4 --only <slug>`。
  带空格的组合问法（「静音 风扇」）更严一档：`hit_count()` 只看标题里真的同时住下这两个词的商品，
  否则描述里凑巧各出现过一次「静音」和「风扇」的交换机也会被算成命中——上一轮就是这样蒙过去了 6 个词。
- `.run/plan_seed_fill.py` 是施工图：每个缺口词只能有一个归属分类，重复分配、分配了站内已有货的词、
  同义词指向的词既没现货也不在数据包里，都会当场报出来。写成品名的数量以它输出的 `.run/seed_spec.json` 为准。
- `.run/lint_seed_data.py` 拦住三类返工：品名没接住分配的词、卖点写成没有数字的空话、
  文案踩了广告法绝对化用语或承诺了本站没有的服务（价保、货到付款、增值税专用发票、上门安装都没有）。
- 说法与品名对不上的那部分用同义词解决，词表只在 `app/services/synonyms.py` 一份，
  `catalog.search()` 与 `catalog.suggest()` 各调一次 `expand()`，前台搜索、联想、小马在线工具与离线路由
  因此共用同一套说法映射。`q_clauses()` 把原话和各说法拼成一条 OR 检索式，列表与分页计数复用同一个
  `where`，所以「共 N 件」不会和翻页结果对不上。
  词表只收「站内确实有货、只是说法不同」的词——品类本身缺货要补货，把「猫砂盆」绕到「猫抓板」上，
  用户看到的结果只会证明这个商城答非所问。`tests/test_synonyms.py` 会把这张表逐个查一遍库，
  右半边缺货就红。牌子说法（乐高、变形金刚、芭比娃娃）也走同义词接到通用品类名，演示商城不替真品牌铺货，
  品名里不出现商标。

### 检索怎么接住「一句话两个条件」和「只说场景不说品类」

- `catalog.tokens()` 按空格切段，段内各说法 OR、段间 AND（`q_clauses()`）。老写法把整句空格抹平当一个
  连续串比，「防水 手表」这种问法会集体 0 件。
- `title_rank()` 数标题命中了几段，只在 `sort="relevant"` 时插到排序最前一级：「静音 风扇」光靠 AND 会把
  描述里同时出现这两个词的交换机捞进来，相关性权重救不了，标题对题的件数才可信。
  `recommend.by_budget()` 带说法时也改走 relevant，所以小马按场合推荐出的是露营灯不是评分更高的电饭煲。
- 标题中间被修饰字插开的说法由 `synonyms.gap_pattern()` 兜底：用户搜「折叠桌」，站内那件叫「Terra 折叠露营桌」，
  整串 `LIKE '%折叠桌%'` 比不中。这不是换了叫法、词表补不了，是把词按原字序拆成 `%折%叠%桌%` 再比一次。三条边界：
  **只比标题**（拿去比描述的话，「毛衣」会捞出「婴儿毛巾」）；**纯汉字且满 3 字才启用**（「雨伞」两个字不拆，
  拆出来是雨具加伞具的乱并；带字母数字的截出来是半截词）；**宽松只用来多捞、不用来排座次**（整串命中计 2 分、
  插字命中计 1 分，搜「台式机」时台式饮水机排在真台式机后面）。筛选式与排序式共用 `_segment_terms()`，
  靠插字进来的商品不会因为排序漏算而集体沉底。
  用例在 `tests/test_synonyms.py` 末段（14 个插字说法逐个查头名品名对不对得上），
  `.run/check_coverage.py` 的「标题插字的说法」组同口径复查。
- 口语框由 `without_generic()` 剥：`GENERIC_TAILS`（装备、用品、礼物、工具……）与 `GENERIC_HEADS`
  （户外、居家、车载……）。站内没有一件商品把自己写成「装备」，「露营装备」真正带货的是「露营」，
  而 tags / attributes 里写的就是这两个字。剥完再走一层同义词，「猫咪用品」才接得到「猫砂盆」。
  两段问法里只由泛词组成的那段直接丢掉（「露营 装备」的「装备」留着 AND 就清零），
  但单搜「装备」不丢——商品标签里确实写着它，丢了就变成全站 6480 件。
  前缀段也不丢：用户单独打出「办公」时那是真条件，「办公 椅」不等于「椅」。
  这套判断的用例在 `tests/test_multitoken_search.py` 与 `tests/test_scene_phrases.py`。
- 需求说法里真缺货的那部分不靠词表绕：孕产护理（`maternal_care`）是第三轮新开的分类，
  待产包、产后修复束缚带、孕期营养零食这些品名先有货，「坐月子 → 产妇」这类同义词才允许写进表。

## 测试

```bash
python -m pytest tests -q -p no:warnings
```

用例打的是本机 MySQL：每个用例注册一次性 `pytest-*` 账号，结束时按子表到主表删干净，
游客会话和空购物车也会一并回收，不动演示数据。覆盖页面渲染与权限（含商品介绍四块内容与别人的订单 404）、
加购/报价/下单/支付/退款全链路、小马意图路由与十个工具的返回结构、订单号问答能否点开详情页。
`client` 与 `admin_client` 分别是前台、后台两个进程的 ASGI 客户端：用例既核对两侧路由互不重叠，
也走一遍真实路径 —— 在前台登录管理员、把同一个 cookie 带到后台端口，`/admin` 与 `/api/admin/*` 要 200；
买家会话带到后台端口只能拿 403。
超时未付也有用例：造一单把 `expires_at` 挪到一小时前，`release_expired()` 必须把它置成 `expired`、
`reservations` 明细一条不剩地置 `released`、可售数回到下单前、写一条 `expired` 事件，
再扫一遍不能返回重复处理、更不能把库存多放一次。
`tests/test_outfit.py` 盯的是穿搭这条链：词表里写给用户看的场合必须真有现货、推荐的每件单品都得在库上标了这个场合、
表单里 `height=` 这种空数字不能被 FastAPI 判成 422、映射到别的场合时回复要说明；
还盯数据够不够凑一套——主件写了几个 `搭配位`，婚礼的男装与女装就得每个位子都有现货，13 个新增品名各查一遍可售库存。
「身高185体重160预算1000」这句原话也进了用例：`height_cm` 必须是 185，回复里不能出现 160cm。
`tests/test_visual.py` 盯的是按图找相似这条链：同一张图自相似度必须是 1.0、同色系要比分色系排得高、
酒红不能被判成「绿色系」、白背景必须被剔掉（否则两张毫不相干的棚拍图都长得像）、太小或不是图片的要拒收、
全站每张在售商品图都得有指纹、第二次 `build()` 只跳过不重算、返回的卡片必须是可售有货的、
回复里必须写明「站内没有款式、品牌或材质的图像识别」，也不许断言「同款」——
但禁的只是肯定说法。`_claims_same()` 按句号/问号/叹号/分号/换行切句，再看命中词左边有没有
「不/没/非/别/未/难/无法」，所以「这个相似度只算接近，不算同款」和
「不代表是同一款或同款商品」这两句老实话都放行；这条规则本身有用例正反各测一遍，
只往前看六个字会把后者误判成违规（浏览器脚本用的是同一条规则）；
接口侧还测上传 happy path 与预览图能 200、类型不符 400、超大小 413、坏文件 400，
以及 `/api/agent/messages` 带 `image_token` 时工具标签是「按图找相似」、`agent_images` 的 `thread_id` 确实回填了。
只发图时前端气泡里补的那句「这张图在站内有相似的吗」和后端 `agent_api.IMAGE_ONLY_QUESTION` 各写了一份，
所以有一条用例逐字比对两边，防止改一处忘一处导致用户看到的和后台记的对不上。
传图频控是每分钟 6 次，模块里有个 autouse 用例把 `agent_api._hits` 清空，否则第七条会被 429 打掉、看着像功能坏了。
用例结束会删掉自己上传的文件与 `agent_images` 行，不碰商品指纹。

`tests/conftest.py` 会在建应用前把 `DEEPSEEK_API_KEY` 置空，所以 pytest 跑的永远是离线路由：
在线分支要花钱、同一句话每次答案还不一样，放进断言里会变成随机失败。
但在线图本身的确定性行为不用花 Key 也能测：`tests/test_agent.py` 用按脚本回答的假 LLM 跑真 LangGraph，
核对一次 `think` 只收到开头那一条 system 消息（尾部不再补「可用工具」清单）、
模型原样重复调同一个工具时两次 `tool_call_id` 各有对应 ToolMessage 但只写一条 `agent_tool_calls`，
以及复用那一跳拿到的确实是带「同一份结果」提示的缓存。
登录态那条也在这里测：`_messages()` 给登录账号的要带「order_no 留空」、给游客的要写「这一轮是游客」，
两边互不串，且说明是拼进同一条 system 而不是再开一条。
还有一条直接调真工具的用例：同参数两次只记一次、换个 `limit` 必须重新查（不能被缓存挡死）、
复用结果不能重复收同一张商品卡。这类用例都在 `contextvars.Context().run()` 里跑完就丢，
否则 `tools.current_thread` 会把已删除的会话编号漏给后面的用例，让它们在 FK 上撞出一堆假失败。
商品卡这条链也有用例兜着：假 LLM 跑完在线那一轮后把 `calls` 落库、再从 `store.cards()` 读回来，
要求链接与当轮一致、字段不越出 `tools.CARD_KEYS`；另一条专门塞一行「preview 是标题字符串」的旧格式，
要求回放返回空而不是造出点不开的卡。`/api/agent/history` 有用例逐条核对卡片对象，并真的 GET 那张卡的 `url` 拿 200。
`test_outfit.py` 盯穿搭卡：每件都要带 `role`，且 `preview` 的链接序列与 `cards` 同源；
前端那两个关键字（`/api/agent/history`、`chat-role` 及它的样式）由一条读源码的用例守住。
`tests/test_synonyms.py` 盯同义词这条链：原话必须排在替换说法前面（两条说法各查一次，不能被拼成一句谁也搜不到的话）、
词表右半边的每个站内用词都要查得到现货、`/shop` 搜索页与 `/api/suggest` 对同一个口语说法给的结果要一致、
「共 N 件」与翻页结果不能各说各话。

补货这轮的前台验收在 `.run/check_restock.py`：直连库数分类与商品数、确认库里的样例订单还在，
再用 Playwright 挨个看导航下拉（73 个分类的面板宽 720 高 540，不铺满屏也不溢出）、侧栏限高滚动
（可视 585px / 内容 1990px，倍率超过 4 就报问题）、15 个日常词的搜索结果、鞋类的规格组不能出现 S/M/L、
新品类不能用占位图，三张截图落在 `.run/shots/`。有问题退出码 1。
在线那条路径仍然要花钱核对，用脚本对着真 DeepSeek 跑：

```bash
python -m uvicorn app.main:app --host 127.0.0.1 --port 3100        # 另开一个终端
python -m uvicorn app.admin_app:app --host 127.0.0.1 --port 3101   # 后台会话列表在 3101
python -X utf8 .run/check_online.py
```

脚本会问库存、推荐、配送、价保、能力边界，再用测试账号下的订单号问进度，逐轮打印 `model` / `degraded` /
命中的工具与回答；后台那一段在前台登录管理员后拿同一个 cookie 去 3101 拉会话列表，
同时确认未登录拿到 403、同一地址打到前台 3100 拿到 404。
除了打印还钉死四组断言：只写在工具 docstring 里的三条口径在线得真读得到（`height_cm` 填 185 不是 160、
用户没报体型就不许补 `body_type`、预算要说清是单件上限），登录账号问「我的订单」必须真调 `query_order`，
游客同一问不许查库、只引导给订单号或登录。
最后按「出自在线大脑的轮次 N/M」判成败：Key 失效或 DeepSeek 欠费（402）时每轮都退回离线路由，
回答照样通顺、脚本却会一片绿，所以一轮在线都没有就直接退出码 1，别拿这种假通过当验收。
前台渲染另有一个浏览器脚本：

```bash
python -X utf8 .run/check_online_ui.py
```

穿搭页另有一个浏览器脚本，检查表单提交、结果卡片、商品图是否真加载、导航入口与详情页「凑一套」链接：

```bash
python -X utf8 .run/check_outfit.py
```

截图落在 `.run/shots/20~26-outfit-*.png`，控制台报错与失败请求会被收集，末尾打印「无控制台/网络错误」才算过。脚本里跑的是两类典型穿搭问法：185cm 男生问婚礼、单件 1000 元以内，要五个穿搭位都配上且图不挂；温柔风 160cm 小个子问婚礼，两套都得有包袋。

它带着测试买家的登录态在悬浮窗里提问，检查在线回答的工具标签写成「订单查询 · 1 笔订单」、
订单号确实变成 `/order/{订单号}` 链接、推荐轮次渲染出商品卡片，截图落在 `.run/shots/33~35-*.png`。
在线轮次的意图记为 `llm`，后台会话列表里显示成「大模型作答」。

按图找相似另有一个浏览器脚本，在 `/support` 与首页浮窗各走一遍真实上传（先起服务，这条走的是在线大脑）：

```bash
python -X utf8 .run/check_visual.py
```

脚本会检查附加按钮与待发送缩略图、发一句「有没有类似的外套」后工具标签是「按图找相似」且卡片上有「相似 N%」、
只发图不打字也能出结果、商品图没有加载失败、选 `.txt` 时对话区出现红色错误气泡（这类文件客户端就挡下，不发请求），
并按 `claims_same()` 断言回答里没有「这就是同款」那种肯定说法；
粘贴那条在页面里合成 `ClipboardEvent` 测几件事：往输入框粘纯文字不能被拦（`defaultPrevented` 必须是 false、
不许冒出待发送缩略图），粘带文件名的图缩略图显示这个名字，粘没有文件名的截图（剪贴板截图就是这样）顶出
「粘贴的截图.png」；再把粘出来的这张真发出去，确认在线那一轮确实调了 `find_similar_by_image`。
悬浮窗还要确认 document 上那个监听没有越界：面板关着时往 `body` 粘贴必须原样放行，
面板开着但焦点在标题栏时粘贴要生效，生效后用「移除」能把待发送的图撤掉。
合成事件用的 `DataTransfer` 在 Chromium 里可以 `items.add(File)`，所以整条链路（校验 → 缩略图 → 上传 → 找相似）都是真的走了一遍；
最后用仓库里自造的管理员账号（另开一个不带浏览器 cookie 的请求通道：在前台 3100 登录，
cookie 不分端口，所以同一个 jar 直接去 3101 取）`/api/admin/agent/threads/{id}`，确认 `images` 里真的带出这张图、
`/img/chat/...` 在后台端口也能打开，然后注销该会话；
再用同一个浏览器在前台登录，页面自动跳到 3101 的 `/admin`，切到「小马会话」点开这条会话，确认 `.admin-images` 真把缩略图画出来且没有加载失败。
每个场景开头都会先抹掉 `localStorage` 里的会话编号再刷新，否则对话区带着上一轮的旧卡片，
「等到出新结果」就变成了「等到页面里本来就有卡片」，等于没测。
结尾清理按 UTC 的 `created_at` 比时间（库里存 UTC，别用本地时间串，那样一条也删不掉），
删掉这次上传的文件、`agent_images` 行与测试会话。截图落在 `.run/shots/27~33-*.png`。

推荐结果的商品卡要有地方点、刷新后还要留得住，这条链另有一个浏览器脚本（走真 DeepSeek）：

```bash
python -X utf8 .run/check_chat_cards.py
```

它在 `/support` 问那句「婚礼、185cm、体重160、单件预算1000」，核对页面画出的卡片数与接口 `cards` 数一致、
每张卡的图真的加载出来（`naturalWidth > 0`）、标题与价格都在、每张卡都带得出穿搭位（那轮是主件、内搭、配饰、鞋履、包袋），
然后真点第一张，确认地址变成 `/product/{slug}` 且详情页标题就是那张卡的商品；
返回并刷新，要求那一问一答连卡片一起回放出来（`href` 里不能出现 `undefined`）、角标不丢；
再切到首页悬浮窗，用同一个会话编号确认上一轮的卡照样回放，并新问一句库存，确认库存问答只给 1 张卡且不带穿搭位。
工具标签也钉了一下：穿搭那轮必须写成「场合穿搭 · 5 件单品：商品名、商品名」，preview 改成对象后标签不能退化成光杆数字。
结尾删掉本次新建的游客会话，截图落在 `.run/shots/36~38-chat-*.png`；有问题就退出码 1。

订单超时释放也要对着真服务核对（pytest 的 `ASGITransport` 不跑 lifespan，启动事件根本不触发，
那条扫描循环在测试里永远不会转），脚本会占住一个扫描周期：

```bash
python -X utf8 .run/check_expiry.py   # 默认等 60 秒一轮，ORDER_SWEEP_SECONDS 改了这里跟着变
```

它用一个新注册的买家账号下单、把 `expires_at` 直接改到一小时前（等价于「服务开着开着半小时过去了」），
然后等循环自己把单关掉：状态必须变 `expired`、可售数回来、`reservations` 不再有 `active`、
时间线上有 `expired` 那一条，最后删掉这单并复核库存。服务日志里要能看到「释放超时未付订单 N 笔」——
`app/main.py` 里那几行 `log.info` 以前是全静默的（uvicorn 只配它自己的 logger，`"lumen"` 这条没人接手），
现在补了 handler，启动时的商品/订单数和小马用哪个大脑也会打出来。

## 关于旧版 Node 商城

`D:\Qoder cn\Projekt\shop` 是原 Express + SQLite 版本，只作为数据来源和参考存档保留：
它不再启动、不再改代码，3010 端口已停用，数据通过 `app/migrate.py` 迁进 MySQL。
`SESSION_SECRET`、数据库密码、DeepSeek Key 都只存在于 `store/.env`。
