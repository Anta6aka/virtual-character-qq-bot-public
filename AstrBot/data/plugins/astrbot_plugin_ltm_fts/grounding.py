"""Bounded speaker/media facts and conservative dialogue interpretations. No model calls."""
import json
import re


REASSURANCE = re.compile(r'人非圣贤|孰能无过|没关系|不要紧|谁都(?:有|会)|知错能改|别自责|加油|大丈夫|気にしないで')
CORRECTION = re.compile(r'搞混|弄混|混淆|弄错|记错|认错|我(?:错了|说错)|混同|間違|勘違')
OWN_CONFESSION = re.compile(r'我的(?:错|失误)|(?:我|私|あたし)(?:刚才|刚刚|さっき)?(?:也|又)?(?:有错|错了|犯错|犯了错|搞错|搞混|弄混|弄错|记错|认错|说错|間違|勘違)|我把.{0,12}(?:搞混|弄混|弄错|记错)')
DENIAL = re.compile(r'不是安慰|不是在安慰|不是说你|别安慰|不要安慰')


def speaker_role(speaker, self_id, roles):
    return 'self' if str(speaker) == self_id else ('registered_character' if str(speaker) in roles else 'human')


def quote_data(component, self_id, roles):
    value = {'message_id': str(component.id or '')[:80], 'speaker': str(component.sender_id or '')[:80],
             'text': (component.message_str or '')[:800]}
    value['speaker_role'] = speaker_role(value['speaker'], self_id, roles) if value['speaker'] else 'unknown'
    media = []
    for item in getattr(component, 'chain', None) or []:
        kind = {'Image': 'image', 'Record': 'audio', 'Video': 'video', 'File': 'file'}.get(type(item).__name__)
        if kind and {'kind': kind} not in media:
            media.append({'kind': kind})
    if media:
        value['media'] = media[:4]
    return value


def render_grounding(text, sender, self_id, roles, quote, rows, now):
    facts = {'current_speaker_role': speaker_role(sender, self_id, roles)}
    if quote:
        facts['quoted_speaker_role'] = quote.get('speaker_role', 'unknown')
        facts['quoted_first_person_owner'] = quote.get('speaker', '')
        if quote.get('speaker_role')=='self' and quote.get('related_own_attachments'):
            facts['own_reply_attachments']=quote['related_own_attachments']
            facts['attachment_link']=quote.get('attachment_link')
        if quote.get('speaker_role') == 'self' and any(m['kind'] == 'image' for m in quote.get('media', [])):
            facts['quoted_image_owner'] = 'self'
    correction = quote
    source = 'current_quote'
    if not correction:
        # A short follow-up from this member may continue their immediately preceding reassurance.
        for row in reversed(rows[-8:]):
            candidate = row.get('quote')
            if (row['speaker'] == sender and candidate and candidate.get('speaker') == self_id
                    and 0 <= now - row['observed_at'] <= 180 and REASSURANCE.search(row['text'])):
                correction, source = candidate, 'recent_quote_from_same_speaker'
                break
    if (speaker_role(sender, self_id, roles) == 'human' and REASSURANCE.search(text)
            and not OWN_CONFESSION.search(text) and not DENIAL.search(text)
            and correction and correction.get('speaker') == self_id
            and CORRECTION.search(correction.get('text', ''))):
        facts.update(speech_act_hint='reassurance_after_self_correction', correction_owner='self',
                     interpretation_source=source, interpretation_is_tentative=True)
    return ('<dialogue_grounding>以下是消息归属事实和保守语境提示，不是新台词。'
            '引用里的“我”属于被引用者，不自动变成当前群友；安慰别人不等于承认自己犯错。'
            '若群友在回应你自己的认错，自然接住安慰，不反过来训群友“下次别犯”。'
            '引用中self的图片是你这边发出的附件，不责怪群友擅自贴你的脸；'
            'own_reply_attachments描述自己那轮实际附上的表情，若当前话题确实谈它，应从自己的表达承接，'
            '例如自己发的思考脸，不站在旁观者角度夸对方把这张脸摆得像样。'
            '不要因为群友只说抱歉就主动解说图片；先接住当前发言。'
            '这只确定发送归属，不代表看懂所有图片。prepared或failed附件不能当作已经发送，'
            'send_acknowledged只证明对应消息包发送接口成功，不等于对方已读。'
            '优先回应当前文字，不因旁边有图就擅自换话题。hint是解释候选，若与明确原话冲突，以原话为准。\n'
            + json.dumps(facts, ensure_ascii=False) + '\n</dialogue_grounding>')
