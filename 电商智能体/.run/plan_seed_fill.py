"""补货施工图：把覆盖度核对里查不到货的购物词分到具体分类，一个词只能有人接。

跑法：PYTHONIOENCODING=utf-8 python -X utf8 .run/plan_seed_fill.py
    → 校验分配表覆盖 .run/gaps.json 里的每个词（漏分配、重复分配都会报出来），
      并写出 .run/seed_spec.json 给补品名的数据模块当依据。

为什么不直接让 AI 自由加品类：没有这张表，两个模块很容易都去补「跑步机」，
或者补完之后仍有几十个词没人接 —— 用户说的「各种购物需求」就还是接不住。
"""

import io
import json
import re
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 每批 = 一个数据模块文件；分类里 words 就是这个词必须变成站内的品名（可为品名的子串）。
# sort 由这里统一排，避免出现两个分类抢同一个排序位。
# 这些说法站内本来就有货（或这次要补的品名里有），只是用户的话和我们的品名对不上。
# 词表只有一份，落在 `app/services/synonyms.py`，检索链路和这张分配表读同一个来源。
# 商标说法也走这条路：演示商城不替真品牌铺货，用户嘴里的「乐高」接进站内的「积木」就够了。
#
# 组合问法（覆盖度词表里带空格的「属性词 品类词」）在这里写成连写的形状：「静音 风扇」记作「静音风扇」，
# 「长续航 充电宝」记作「长续航充电宝」。落库的品名要同时含住这两个词才算真接住，
# 品类词允许换成站内说法（充电宝 → 移动电源），因为检索时会把同义词一起展开。
from app.services.synonyms import SYNONYMS  # noqa: E402

BATCHES: dict[str, dict] = {

    "appliance_big.py": {
        "major_appliance": {"name": "大家电", "sort": 200, "price": [899, 12999], "words": [
            "空调", "中央空调", "洗衣机", "烘干机", "消毒柜", "热水器", "净水器", "新风系统", "电视", "激光电视", "饮水机",
            "小型冰箱", "节能冰箱", "大容量冰箱", "静音空调"]},
        "kitchen_big": {"name": "厨房大电", "sort": 210, "price": [599, 8999], "words": [
            "油烟机", "微波炉", "电压力锅", "电陶炉", "炒菜机", "咖啡机", "制冰机"]},
        "kitchen_small": {"name": "厨房小电", "sort": 220, "price": [79, 1999], "words": [
            "面条机", "豆浆机", "养生壶", "电热水壶", "多士炉", "三明治机", "磨豆机", "榨汁机", "原汁机",
            "抽真空机", "厨房秤", "蒸锅", "火锅锅", "气泡水机", "静音破壁机"]},
    },
    "life_appliance.py": {
        "home_appliance": {"name": "生活电器", "sort": 230, "price": [59, 3999], "words": [
            "吸尘器", "洗地机", "除螨仪", "挂烫机", "熨斗", "电风扇", "冷风扇", "取暖器", "油汀", "电热毯",
            "暖风机", "除湿机", "大吸力吸尘器", "静音风扇"]},
        "personal_care_appliance": {"name": "个护电器", "sort": 240, "price": [99, 6999], "words": [
            "理发器", "按摩仪", "按摩椅", "足浴盆", "护眼仪", "卷发器"]},
        # 第二轮：电器买回来才发现要换的耗材，站内一件都没有，用户只能空手走
        "appliance_supply": {"name": "家电耗材", "sort": 780, "price": [9, 599], "words": [
            "扫地机耗材", "净水器滤芯", "空气净化器滤网", "洗衣机底座", "冰箱除臭盒"]},
    },
    "pc_office.py": {
        "pc_hardware": {"name": "电脑硬件", "sort": 250, "price": [149, 19999], "words": [
            "台式机", "显卡", "主板", "CPU", "内存条", "机械硬盘", "服务器", "NAS", "UPS 电源", "笔记本电脑", "平板电脑"]},
        "pc_peripheral": {"name": "电脑外设", "sort": 260, "price": [39, 2999], "words": [
            "扩展坞", "手写板", "绘图板", "U 盘", "存储卡", "声卡"]},
        "office_machine": {"name": "办公设备", "sort": 270, "price": [299, 12999], "words": [
            "打印机", "扫描仪", "复印机", "考勤机", "碎纸机", "错题打印机", "标签机"]},
        "network_gear": {"name": "网络设备", "sort": 280, "price": [39, 2999], "words": ["网卡", "交换机"]},
        "accessory": {"__extra__": True, "words": [
            "耳机套", "手机挂绳", "长续航充电宝", "便携充电宝", "防摔手机壳"]},
        "wearable": {"__extra__": True, "words": ["防水手表"]},
        # 第二轮：打游戏的人在这站里连一把电竞椅都找不到
        "game_gear": {"name": "游戏设备", "sort": 770, "price": [19, 4999], "words": [
            "掌机", "电竞椅", "电竞桌", "手柄充电座", "机械键盘轴体"]},
    },
    "media_music.py": {
        "camera_gear": {"name": "摄影器材", "sort": 290, "price": [99, 25999], "words": [
            "单反", "微单", "无人机", "拍立得", "独脚架", "摄影棚", "稳定器",
            "提词器", "遮光罩", "快门线", "快门线遥控", "麦克风支架", "镜头袋", "相机背带"]},
        "tv_av": {"name": "客厅影音", "sort": 300, "price": [199, 9999], "words": ["功放", "唱片", "收音机", "投影幕布", "K 歌", "调音台"]},
        "instrument": {"name": "乐器", "sort": 310, "price": [49, 9999], "words": [
            "吉他", "尤克里里", "电子琴", "钢琴", "口琴", "笛子", "节拍器", "调音器", "乐谱", "音箱线"]},
        "audio": {"__extra__": True, "words": ["无线耳机"]},
    },
    "furniture_bedding.py": {
        "furniture": {"name": "家具", "sort": 320, "price": [99, 12999], "words": [
            "沙发", "茶几", "餐桌", "餐椅", "书桌", "床垫", "衣柜", "鞋柜", "书柜", "置物架",
            "穿衣镜", "凳子", "折叠桌", "懒人沙发", "升降桌", "电脑桌", "电脑椅", "电视柜",
            "学生床垫", "厨房置物架", "可折叠桌椅", "办公椅"]},
        "bedding": {"name": "床品家纺", "sort": 330, "price": [59, 2999], "words": [
            "乳胶枕", "枕头", "被子", "四件套", "床单", "被套", "凉席", "纱帘", "可水洗地毯", "无异味地垫"]},
        "bath_fixture": {"name": "卫浴洁具", "sort": 340, "price": [79, 6999], "words": [
            "马桶", "智能马桶", "花洒", "浴室柜", "毛巾架"]},
        "lighting": {"__extra__": True, "words": ["筒灯", "感应灯"]},
    },
    "kitchen_table.py": {
        "cookware": {"name": "锅具刀具", "sort": 350, "price": [39, 2999], "words": [
            "菜刀", "不粘锅", "铁锅", "砂锅", "高压锅", "案板", "雪平锅", "磨刀器"]},
        "tableware": {"name": "餐具水具", "sort": 360, "price": [9, 999], "words": [
            "餐具", "盘子", "筷子", "勺子", "水杯", "马克杯", "红酒杯", "调料盒", "油壶", "隔热垫",
            "密封罐", "调味瓶", "油瓶", "不锈钢保温杯", "高颜值杯子"]},
        "kitchen_supply": {"name": "厨房耗材", "sort": 370, "price": [5, 199], "words": [
            "保鲜膜", "锡纸", "厨房纸", "食品袋", "围裙", "抹布", "钢丝球", "洗洁精", "沥水架", "刀架", "米桶"]},
        "clean_tool": {"name": "清洁工具", "sort": 380, "price": [6, 599], "words": [
            "马桶刷", "拖把", "扫把", "刮水器", "管道疏通", "洗衣袋", "衣撑", "夹子", "洗衣盆", "搓衣板",
            "垃圾袋", "洗衣机槽", "除湿盒", "樟脑丸", "防霉片", "空气清新剂"]},
        # 第二轮：想在家做蛋糕、寿司的人，站内连一套模具都凑不齐
        "bakeware": {"name": "烘焙工具", "sort": 790, "price": [9, 599], "words": ["烘焙模具", "寿司帘", "擀面杖"]},
    },
    "grocery.py": {
        "dairy_drink": {"name": "乳品冲调", "sort": 390, "price": [9, 299], "words": ["牛奶", "酸奶", "燕麦", "麦片"]},
        "tea_coffee": {"name": "茶叶咖啡", "sort": 400, "price": [19, 999], "words": [
            "咖啡豆", "挂耳咖啡", "速溶咖啡", "茶叶", "绿茶", "红茶", "普洱", "乌龙茶", "花草茶"]},
        "snacks": {"name": "休闲零食", "sort": 410, "price": [6, 299], "words": [
            "巧克力", "糖果", "饼干", "薯片", "坚果", "腰果", "碧根果", "果干", "牛肉干", "猪肉脯",
            "辣条", "蛋糕", "冰淇淋"]},
        "grain_oil": {"name": "粮油调味", "sort": 420, "price": [5, 399], "words": [
            "大米", "面粉", "食用油", "橄榄油", "酱油", "蚝油", "火锅底料", "调味料", "花椒", "八角",
            "白糖", "代糖", "罐头"]},
        "instant_food": {"name": "速冻方便", "sort": 430, "price": [9, 199], "words": [
            "方便面", "米线", "螺蛳粉", "自热米饭", "速冻水饺", "粽子", "汤圆", "月饼"]},
        "beverage": {"name": "饮料", "sort": 440, "price": [3, 299], "words": [
            "气泡水", "矿泉水", "苏打水", "果汁", "功能饮料", "凉茶", "碳酸饮料", "无糖可乐"]},
        "liquor": {"name": "酒类", "sort": 450, "price": [19, 2999], "words": [
            "白酒", "红酒", "葡萄酒", "啤酒", "精酿啤酒", "洋酒", "威士忌", "白兰地", "清酒", "梅子酒", "果酒"]},
    },
    "health_wash.py": {
        "health_food": {"name": "营养保健", "sort": 460, "price": [19, 999], "words": [
            "维生素", "钙片", "鱼油", "益生菌", "褪黑素", "胶原蛋白", "枸杞", "蜂蜜", "阿胶", "参",
            "燕窝", "蛋白粉", "增肌粉", "能量胶"]},
        "medical_device": {"name": "医疗器械", "sort": 470, "price": [19, 9999], "words": [
            "血压计", "血压仪", "血糖仪", "血氧仪", "额温枪", "助听器", "体脂秤", "体温计", "制氧机",
            "轮椅", "拐杖", "护理床", "药箱", "创可贴"]},
        "daily_wash": {"name": "洗护日化", "sort": 480, "price": [6, 299], "words": [
            "洗发水", "护发素", "发膜", "沐浴露", "香皂", "洗手液", "消毒液", "洗衣液", "洗衣凝珠",
            "柔顺剂", "衣领净", "牙膏", "牙线", "漱口水", "棉签", "剃须泡", "止汗露",
            "驱蚊液", "花露水",
            "儿童牙膏", "美白牙膏", "控油洗发水", "去屑洗发水", "除螨洗衣液"]},
        # 第二轮：家里有人受伤或上了年纪，这些是去医院拎回来才发现买漏的
        "care_aid": {"name": "康复护理", "sort": 800, "price": [19, 4999], "words": [
            "颈椎牵引器", "助行器", "坐便椅", "洗澡椅", "护理垫", "防褥疮垫", "按摩枕"]},
    },
    "beauty_extra.py": {
        "beauty": {"__extra__": True, "words": [
            "粉底液", "气垫", "遮瑕", "散粉", "眉笔", "眼线笔", "睫毛膏", "眼影", "腮红", "卸妆",
            "卸妆水", "化妆包", "染发剂", "指甲油", "美甲"]},
        "skincare": {"__extra__": True, "words": ["洗面奶", "化妆水", "乳液", "防晒霜", "隔离霜", "护手霜", "敏感肌洗面奶"]},
        "fragrance": {"__extra__": True, "words": ["淡香水"]},
    },
    "baby_pet_toy.py": {
        "baby_food": {"name": "母婴辅食", "sort": 490, "price": [19, 599], "words": ["奶粉", "米粉", "辅食", "婴儿米粉"]},
        "baby_gear": {"name": "母婴用品", "sort": 500, "price": [15, 4999], "words": [
            "纸尿裤", "拉拉裤", "湿巾", "奶瓶", "奶嘴", "吸奶器", "婴儿床", "婴儿车", "推车", "安全座椅",
            "背带", "腰凳", "学步车", "儿童餐椅", "牙胶", "婴儿浴盆", "婴儿洗衣液", "儿童牙刷",
            "婴儿面霜", "安抚玩具", "儿童座椅",
            "尿布台", "婴儿床铃", "学步带", "咬咬乐", "辅食机", "爬行垫", "门栏", "安抚巾", "婴儿指甲剪"]},
        "toy": {"name": "玩具积木", "sort": 510, "price": [9, 1999], "words": [
            "积木", "拼图", "玩偶", "毛绒玩具", "手办", "盲盒", "轨道车", "遥控车", "无人机玩具",
            "水枪", "泡泡机", "益智玩具", "桌游", "飞行棋", "象棋", "围棋", "五子棋", "模型",
            "变形玩具", "换装娃娃", "识字卡", "编程机器人", "沙滩玩具", "科学实验套装"]},
        "pet_supply": {"name": "宠物用品", "sort": 520, "price": [9, 1999], "words": [
            "猫粮", "狗粮", "猫砂", "猫罐头", "狗零食", "磨牙棒", "牵引绳", "胸背带", "项圈", "猫爬架",
            "猫抓板", "宠物窝", "航空箱", "宠物玩具", "宠物饮水机", "猫砂盆", "尿垫", "驱虫",
            "宠物沐浴露", "狗粮勺",
            "宠物推车", "伊丽莎白圈", "吹水机", "冻干零食", "喂食器", "猫砂垫", "宠物围栏"]},
    },
    "stationery_book.py": {
        "stationery": {"name": "文具书写", "sort": 530, "price": [3, 599], "words": [
            "笔", "中性笔", "钢笔", "圆珠笔", "铅笔", "自动铅笔", "马克笔", "荧光笔", "白板笔", "橡皮",
            "尺子", "圆规", "剪刀", "美工刀", "胶带", "订书机", "计算器", "手账", "便签", "书签",
            "笔袋", "印泥", "笔记本"]},
        "office_storage": {"name": "办公收纳", "sort": 540, "price": [15, 1999], "words": ["文件夹", "文件柜", "档案盒", "白板"]},
        "book": {"name": "图书音像", "sort": 550, "price": [9, 299], "words": ["图书", "小说", "绘本", "字典", "字帖", "漫画", "杂志", "绘本架"]},
        "school_bag": {"name": "学生用品", "sort": 560, "price": [19, 599], "words": [
            "书包", "地球仪", "大容量书包", "电子单词卡", "阅读架"]},
    },
    "sports_outdoor.py": {
        "ball_sport": {"name": "球类运动", "sort": 570, "price": [19, 2999], "words": [
            "篮球", "足球", "排球", "台球", "乒乓球拍", "网球拍"]},
        "gym_machine": {"name": "健身大器械", "sort": 580, "price": [99, 12999], "words": [
            "跑步机", "动感单车", "椭圆机", "划船机", "力量器械", "仰卧板", "健腹轮", "杠铃", "护腕",
            "哑铃凳", "卧推架"]},
        "swim_gear": {"name": "游泳装备", "sort": 590, "price": [19, 999], "words": ["泳帽", "泳衣", "浮板", "救生衣"]},
        "skate_board": {"name": "轮滑滑板", "sort": 600, "price": [39, 4999], "words": ["轮滑鞋", "滑板", "滑板车", "平衡车"]},
        "camping": {"__extra__": True, "words": [
            "露营椅", "户外炉具", "气罐", "保温桶", "头灯", "对讲机", "指南针", "望远镜", "野餐垫",
            "工兵铲", "防雨布", "冰爪", "营地车", "套锅", "净水片", "户外帐篷", "防风帐篷"]},
        "fitness": {"__extra__": True, "words": [
            "运动水壶", "速干衣", "运动内衣", "握力器", "呼啦圈", "瑜伽球", "泡沫轴", "拉筋板",
            "速干裤", "速干裤子"]},
        # 第二轮：钓鱼佬是这站里最明显的空白，八个词一个都接不住
        "fishing": {"name": "垂钓装备", "sort": 760, "price": [19, 1999], "words": [
            "鱼竿", "钓鱼椅", "路亚饵", "渔线", "鱼钩", "打窝料", "抄网", "鱼护"]},
    },
    "apparel_extra.py": {
        "underwear": {"name": "内衣袜品", "sort": 610, "price": [9, 599], "words": [
            "文胸", "内裤", "睡衣", "家居服", "袜子", "船袜", "丝袜", "保暖内衣", "浴袍", "秋衣秋裤"]},
        "travel_gear": {"name": "旅行用品", "sort": 620, "price": [9, 999], "words": [
            "旅行套装", "洗漱包", "颈枕", "行李牌", "压缩袋", "防水袋", "转换插头", "万能插头", "便携秤",
            "折叠伞", "雨伞", "雨衣", "保温包", "野餐篮", "防晒雨伞"]},
        "apparel": {"__extra__": True, "words": [
            "毛衣", "针织衫", "棉服", "软壳", "运动裤", "短裤", "工装裤", "百褶裙", "半身裙", "背带裤",
            "羊绒衫", "阔腿裤", "背带裙", "孕妇装", "大码女装", "中老年女装", "厨师服", "白大褂",
            "抓绒衣", "冲锋裤", "皮肤衣", "高腰牛仔裤"]},
        "fashion": {"__extra__": True, "words": [
            "运动鞋", "雪地靴", "切尔西靴", "凉鞋", "豆豆鞋", "篮球鞋", "羽毛球鞋", "登山鞋", "越野鞋",
            "洞洞鞋", "雨鞋", "德训鞋"]},
        "accessory": {"__extra__": True, "words": [
            "帽子", "棒球帽", "针织帽", "鞋刷", "增高鞋垫", "领结", "袖扣", "贝雷帽", "冰袖"]},
        "bags": {"__extra__": True, "words": [
            "双肩包", "单肩包", "手提包", "胸包", "帆布包", "电脑包", "钱包", "卡包", "钥匙包",
            "环保袋", "登机箱", "旅行包", "证件包", "防水背包"]},
    },
    "garden_gift.py": {
        "garden": {"name": "园艺绿植", "sort": 630, "price": [5, 2999], "words": [
            "绿萝", "多肉", "花盆", "营养土", "园艺工具", "洒水壶", "花肥", "种子", "树苗", "草坪",
            "割草机", "修枝剪", "喷壶", "花架", "庭院灯", "仿真花", "鲜花", "干花", "花瓶", "水培植物"]},
        "gift": {"name": "礼品节庆", "sort": 640, "price": [5, 999], "words": [
            "生日礼物", "情人节礼物", "伴手礼", "礼盒", "贺卡", "气球", "包装纸", "丝带", "节日装饰",
            "圣诞", "春联", "灯笼", "红包", "婚庆用品", "生日帽", "对联", "窗花"]},
        "package_supply": {"name": "包装耗材", "sort": 650, "price": [5, 399], "words": [
            "纸箱", "气泡膜", "封箱胶带", "打包带", "标签纸", "快递袋"]},
    },
    "jewelry_eyewear.py": {
        "jewelry": {"name": "珠宝首饰", "sort": 660, "price": [29, 9999], "words": [
            "项链", "手链", "手镯", "耳环", "耳钉", "胸针", "脚链", "银饰", "珍珠", "玉石"]},
        "eyewear": {"name": "眼镜墨镜", "sort": 670, "price": [39, 2999], "words": [
            "墨镜", "近视镜", "防蓝光眼镜", "镜框", "隐形眼镜", "眼镜盒"]},
    },
    "car_fix_security.py": {
        "car_supply": {"name": "汽车用品", "sort": 680, "price": [15, 2999], "words": [
            "车载充电器", "行车记录仪", "车载支架", "汽车脚垫", "座垫", "方向盘套", "车载香水",
            "洗车液", "车蜡", "充气泵", "搭电宝", "安全锤", "遮阳挡", "雨刮器", "机油",
            "洗车机", "镀膜剂", "车内收纳", "车载手机支架"]},
        "security_gear": {"name": "安防锁具", "sort": 690, "price": [19, 3999], "words": [
            "监控摄像头", "智能门锁", "门铃", "可视门铃", "保险箱", "报警器", "灭火器", "烟雾报警器",
            "燃气报警器", "防身", "挂锁", "门锁"]},
        "hardware_tool": {"name": "五金工具", "sort": 700, "price": [9, 2999], "words": [
            "螺丝刀", "扳手", "钳子", "锤子", "电钻", "冲击钻", "电动螺丝刀", "卷尺", "水平仪", "梯子",
            "人字梯", "工具箱", "合页", "把手", "便携电钻", "美缝剂", "玻璃胶", "窗帘轨道", "罗马杆"]},
        "electrical": {"name": "电工电料", "sort": 710, "price": [5, 999], "words": [
            "水管", "水龙头", "角阀", "生料带", "电工胶带", "插线板", "充电电池",
            "灯管", "万用表", "电笔", "开关面板"]},
    },
    # 最后一批是「家里缺了才发现」的小东西和两项运动：覆盖度词表里新加的 30 个词落在这里
    "everyday.py": {
        "paper_goods": {"name": "纸品卫生", "sort": 720, "price": [5, 299], "words": [
            "卫生纸", "卷纸", "抽纸", "湿厕纸", "棉柔巾", "卫生巾", "护垫", "夜安裤", "成人纸尿裤"]},
        "cycling": {"name": "骑行装备", "sort": 730, "price": [19, 6999], "words": [
            "山地车", "公路车", "儿童自行车", "骑行服", "骑行手套", "打气筒", "自行车锁", "骑行水壶"]},
        "aquarium": {"name": "水族观赏", "sort": 740, "price": [19, 1299], "words": [
            "鱼缸", "过滤泵", "鱼食", "水族灯", "氧气泵", "底砂", "渔网"]},
        "small_goods": {"name": "日用小件", "sort": 750, "price": [5, 399], "words": [
            "闹钟", "梳子", "针线盒", "镜子", "挂钟", "指甲刀"]},
    },
    # 第三轮：用户不说品类、只说「我是谁、要干什么」的那批说法。
    # 新分类只开孕产护理一个（待产这块原本一件没有），其余全往已有分类挂品名。
    "scene_phrases.py": {
        "maternal_care": {"name": "孕产护理", "sort": 810, "price": [19, 499], "words": [
            "待产包", "产后修复", "孕期营养", "产假用品"]},
        "gift": {"__extra__": True, "words": [
            "送妈妈", "送爸爸", "送老师", "送同事", "退休礼物", "老人过寿", "乔迁礼物", "婚礼伴手礼"]},
        "toy": {"__extra__": True, "words": ["团建游戏"]},
        "camping": {"__extra__": True, "words": ["自驾露营", "亲子游"]},
        "garden": {"__extra__": True, "words": ["阳台种菜", "花园工具"]},
        "small_goods": {"__extra__": True, "words": ["搬家打包"]},
        "stationery": {"__extra__": True, "words": ["开学文具"]},
        "pet_supply": {"__extra__": True, "words": ["猫咪用品", "狗狗外出", "宠物出行"]},
        "baby_gear": {"__extra__": True, "words": ["新生儿用品"]},
        "baby_food": {"__extra__": True, "words": ["婴儿辅食"]},
    },
}


def flat(text: str) -> str:
    """比对用的形状：空格、斜杠、顿号、括号都不算差别，「静音 风扇」和「静音风扇」是同一个词。"""
    return re.sub(r"[\s/、，,（）()]+", "", text).lower()


def main() -> int:
    gaps = json.loads((ROOT / ".run/gaps.json").read_text(encoding="utf-8"))
    assigned: dict[str, list[str]] = {}
    label: dict[str, str] = {}
    for module, cats in BATCHES.items():
        for slug, spec in cats.items():
            for w in spec["words"]:
                key = flat(w)
                assigned.setdefault(key, []).append(f"{module}:{slug}")
                label.setdefault(key, w)

    problems = []
    gap_keys = {flat(w): w for w in gaps}
    unassigned = [k for k in gap_keys if k not in assigned and gap_keys[k] not in SYNONYMS]
    dup = {k: v for k, v in assigned.items() if len(v) > 1}
    # 同义词指向的那个词也要真有人铺货：写在 words 里当分配词，就是让数据包必须出这个品名
    targets = {flat(t) for t in SYNONYMS.values()}
    from app import seed_data

    planned = {flat(n) for n in seed_data.all_names()}
    # 这张表会一轮一轮往下攒，上一轮已经落成品名的词不算「重复补」；
    # 只有既不在本轮缺口、又不是同义词目标、数据包里也变不出这个品名的词才是真问题。
    unknown = [k for k in assigned if k not in gap_keys and k not in targets
               and not any(k in n for n in planned)]
    sorts: dict[int, list[str]] = {}
    for module, cats in BATCHES.items():
        for slug, spec in cats.items():
            if "sort" in spec:
                sorts.setdefault(spec["sort"], []).append(f"{module}:{slug}")
    for s, where in sorted(sorts.items()):
        if len(where) > 1:
            problems.append(f"排序位撞车：{s} → {where}")
    for k in unassigned:
        w = gap_keys[k]
        problems.append(f"没人接：{w}（{gaps[w]}）")
    for k, v in dup.items():
        problems.append(f"重复分配：{label[k]} → {v}")
    for k in unknown:
        problems.append(f"这个词站内已经有货，别重复补：{label[k]}")

    # 同义词只是把说法接上，被指向的那个词必须查得到东西：站内已有现货，
    # 或者就在这次要灌的数据包里（补货之后才有货的那批）。两个都没有就是拿 0 件换另一个 0 件。
    from app.services import catalog

    for word, target in SYNONYMS.items():
        needle = flat(target)
        covered = catalog.search(q=target, per_page=1)["total"] > 0 or any(needle in n for n in planned)
        if not covered:
            problems.append(f"同义词「{word}」指向的「{target}」站内没货、数据包里也还没有")
        if flat(word) in assigned:
            problems.append(f"「{word}」既建了同义词又分了品类，只留一条路")

    out = {"gaps_total": len(gaps), "synonyms": SYNONYMS, "batches": BATCHES}
    (ROOT / ".run/seed_spec.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")

    words = sum(len(spec["words"]) for cats in BATCHES.values() for spec in cats.values())
    cats_total = sum(len(cats) for cats in BATCHES.values())
    print(f"分配表：{len(BATCHES)} 个模块 / {cats_total} 个分类条目 / {words} 个品名词 + {len(SYNONYMS)} 个同义词")
    if problems:
        print(f"\n问题 {len(problems)} 条：")
        for p in problems[:60]:
            print("  " + p)
        return 1
    print(f"校验通过：{len(gaps)} 个缺口词一个不落，各归一个分类或由同义词接住。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
