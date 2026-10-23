"""Local concept expansion for candidate retrieval, not a pretrained embedding model.

Candidates remain quotations with source/time. A match never establishes a fact.
"""
import re

CONCEPTS = (
    ('面试','应聘','求职','找工作','招聘','interview','面接'),
    ('考试','测验','期末','应试','考核','exam','試験'),
    ('猫','猫咪','喵','cat','ねこ','ネコ'),
    ('狗','狗狗','汪','dog','いぬ','犬'),
    ('生日','诞辰','生辰','birthday','誕生日'),
    ('游戏','打游戏','打机','game','ゲーム'),
    ('失眠','睡不着','睡不着觉','insomnia','眠れない'),
    ('加班','工作到很晚','工作到深夜','overtime','残業'),
    ('生病','不舒服','身体不适','illness','体調'),
    ('旅行','旅游','出游','travel','旅行'),
    ('毕业','离校','graduation','卒業'),
    ('搬家','迁居','换住处','moving house','引っ越し'),
    ('约定','说好了','答应','promise','約束'),
)

def concepts(text):
    text=text.lower()
    return {i for i,words in enumerate(CONCEPTS) if any(
        re.search(r'(?<![a-z])'+re.escape(word)+r'(?![a-z])',text) if word.isascii()
        else word in text for word in words)}

def concept_score(query,text):
    left,right=concepts(query),concepts(text)
    return 0.35*len(left & right)/len(left | right) if left and right else 0.0
