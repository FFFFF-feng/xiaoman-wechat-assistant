"""Reviewed IDs only; the manifest cannot register arbitrary media at runtime."""
PHOENIX = {
    'morning': '早呀', 'received': '收到', 'cheer': '加油', 'rest': '歇一下',
    'thanks': '谢谢你', 'goodnight': '晚安', 'laugh': '哈哈哈', 'hug': '抱抱',
    'here': '在呢', 'sad': '委屈', 'okay': '好哒', 'comfort': '辛苦啦',
    'wait': '等等我', 'go': '出发',
}
FLUENT = {
    'fluent_cat_hello': '猫猫探头', 'fluent_cat_laugh': '猫猫开心',
    'fluent_cat_cry': '猫猫委屈', 'fluent_cat_love': '猫猫喜欢',
    'fluent_hug': '3D抱抱', 'fluent_tears': '忍住眼泪', 'fluent_plead': '可怜巴巴',
    'fluent_party': '庆祝一下', 'fluent_salute': '3D收到', 'fluent_thumb': '给你点赞',
    'fluent_clap': '鼓掌鼓掌', 'fluent_thanks': '3D谢谢', 'fluent_cheer': '打起精神',
    'fluent_wave': '挥挥手', 'fluent_relax': '松口气', 'fluent_peek': '偷偷看看',
    'fluent_sleep': '困困了', 'fluent_cool': '有点小酷', 'fluent_heart': '给你爱心',
    'fluent_dog': '狗狗陪你', 'fluent_rabbit': '兔兔陪你', 'fluent_panda': '熊猫陪你',
    'fluent_coffee': '喝口热的', 'fluent_laugh': '笑出眼泪',
}
CAPTIONS = {**PHOENIX, **FLUENT}
STYLES = {'mixed': '混合搭配', 'phoenix': '只用小凤鸟', 'fluent': '只用3D表情'}
SCENES = {
    'comfort': ['hug', 'comfort', 'here', 'fluent_hug', 'fluent_heart', 'fluent_dog', 'fluent_rabbit', 'fluent_panda'],
    'sad': ['sad', 'hug', 'fluent_cat_cry', 'fluent_tears', 'fluent_plead', 'fluent_hug'],
    'tired': ['comfort', 'rest', 'hug', 'fluent_relax', 'fluent_sleep', 'fluent_coffee'],
    'morning': ['morning', 'fluent_wave', 'fluent_coffee'],
    'night': ['goodnight', 'fluent_sleep'],
    'thanks': ['thanks', 'fluent_cat_love', 'fluent_thanks', 'fluent_heart'],
    'happy': ['laugh', 'cheer', 'okay', 'fluent_cat_laugh', 'fluent_party', 'fluent_clap', 'fluent_cool', 'fluent_laugh'],
    'celebrate': ['cheer', 'okay', 'fluent_party', 'fluent_thumb', 'fluent_clap', 'fluent_cool'],
    'cheer': ['cheer', 'okay', 'fluent_thumb', 'fluent_clap', 'fluent_cheer'],
    'wait': ['wait', 'fluent_peek'],
    'go': ['go', 'cheer', 'fluent_salute', 'fluent_cheer', 'fluent_wave'],
    'hello': ['here', 'hug', 'received', 'fluent_cat_hello', 'fluent_wave', 'fluent_dog', 'fluent_rabbit', 'fluent_panda'],
    'okay': ['okay', 'received', 'fluent_cat_hello', 'fluent_salute', 'fluent_thumb'],
    'neutral': ['here', 'received', 'okay', 'fluent_cat_hello', 'fluent_wave', 'fluent_dog', 'fluent_rabbit', 'fluent_panda'],
    'love': ['hug', 'thanks', 'fluent_cat_love', 'fluent_hug', 'fluent_heart'],
}


def matches_style(sticker, style):
    return sticker in (CAPTIONS if style == 'mixed' else FLUENT if style == 'fluent' else PHOENIX)


def scene_for(sticker):
    return {
        'morning': 'morning', 'received': 'okay', 'cheer': 'cheer', 'rest': 'tired',
        'thanks': 'thanks', 'goodnight': 'night', 'laugh': 'happy', 'hug': 'comfort',
        'here': 'hello', 'sad': 'sad', 'okay': 'okay', 'comfort': 'comfort',
        'wait': 'wait', 'go': 'go',
    }.get(sticker, 'neutral')
