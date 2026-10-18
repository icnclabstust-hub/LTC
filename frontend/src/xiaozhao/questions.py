"""以問句為單位切開一段文字（2026-10-14 question-window 設計文件第 4 節、第 8 節）。

流程：ASR 依停頓補「，」→ 標點模型加標點 → 以整句（句號、問號、驚嘆號之間）判斷是否為問句 →
找出問句從哪個小句開始 → 依序切成「陳述段」與「問句」。

問句規則依教育部《重訂標點符號手冊》修訂版（問號用於疑問句之後；強烈的反詰疑問句用驚嘆號）與
《國語辭典簡編本》「嗎」「呢」「吧」的釋義，並依提案學生審閱 tk01～04 與 30 句測試句的結果補充。
"""

import re
from collections.abc import Callable
from dataclasses import dataclass

_PUNCT = re.compile(r"[\s，,。．.？?！!、；;：:「」『』（）()…~～-]")
_TERMINAL = "。？！?!"
_CLAUSE = "，,、；;"

WH = re.compile(r"誰|什麼|甚麼|哪|怎麼|怎樣|為什麼|為何|多少|多久|如何"
                r"|幾[點個顆次天歲號年月分位樓床瓶包粒杯碗週周度]|幾(公斤|公分|公克|毫升|西西)")
AXNA = re.compile(r"有沒有|是不是|能不能|可不可以|要不要|會不會|對不對|行不行|需不需要|([一-鿿])不\1")
# 「請問」之後的整段是問句；「A 還是 B」選擇問（「還是很／一樣／沒／不／都」是副詞「仍然」）；句尾「了沒」正反問
ASK_START = re.compile(r"請問")
CHOICE = re.compile(r"(?<=.)還是(?!很|一樣|沒|不|都|有點)")
TAIL_NOT = re.compile(r"(了|過)沒有?$")
# 疑問詞的非疑問用法：任指（什麼都、有什麼…就、怎麼…都）、虛指（好幾、這幾、多…幾）
NOT_WH = re.compile(r"什麼都|怎麼.{0,8}都|哪裡都|誰都|多少都|沒什麼|不知道|什麼的|有什麼.{0,8}就|(好|這)幾|多.{0,6}幾")
# 間接問句：「問、看看、要看、知道、確認」之後接疑問結構、句尾沒有「嗎」時是陳述（我去問護理站有沒有手套）
INDIRECT = re.compile(r"((?<!請)問|看看|要看|知道|確認|查|打聽).{0,10}(有沒有|是不是|能不能|可不可以|要不要|會不會|怎麼|什麼|多少|幾)")
# 「如果／要是…呢」：省略的問句（那如果他半夜又跌倒呢）
IF_NE = re.compile(r"(如果|要是|萬一|假如).*呢$")
# 詢問對方意見：「你覺得呢」（沒有疑問詞的「呢」原則上是陳述，這類例外算問句）
ASK_OPINION = re.compile(r"(你|妳|您)(覺得|認為|看|想)(怎麼樣|如何)?呢$")
# 口語爭吵、反問（提案學生判定列入情緒表達，不算問句）：
#   「…懂什麼」（「想知道什麼」「要知道什麼」除外）、「不要／別…好不好」、「難道…」
ARGUE_WHAT = re.compile(r"(?<!想)(?<!要)(懂|知道|管|算|在乎)個?(什麼|甚麼)$")
ARGUE_TAG = re.compile(r"(不要|別|少|不准).{0,15}(好不好|行不行)$")
RHETORICAL = re.compile(r"難道")
TAIL = re.compile(r"[啊呀喔哦嗯欸耶啦]+$")
# 問句前的引導詞（另外機構可以幫忙代購嗎）：併入後面的問句
# 問句缺主詞、前一小句話沒說完：往前併入（發燒的時候，可以讓她洗頭嗎；看牙醫要，請誰陪同）
# （2026-10-14 外接麥克風錄音盲測：停頓逗號把問句從中間切開）
NO_SUBJECT = re.compile(r"^(要|可以|能|會|需要|該|應該|請|得)")
DANGLING = re.compile(r"(的時候|時候|的話|要|會|想|可以|需要)$")
# 句子明顯沒說完（猶豫的長停頓被補成句號）：「可以幫我把一條。乾淨的毛巾拿過來嗎」→ 併入下一句
UNFINISHED = re.compile(r"(的時候|時候|的話|要|會|想|可以|需要|幫我|把[^，。]{0,6}|一[條個些張份件杯碗]|跟|和|因為|如果|請問)$")
MAX_EXTEND = 2
LEAD = re.compile(r"^(另外|還有|那|那麼|所以|然後|對了|啊對了)$")

# ASR 字與字的間隔達到此秒數就補「，」（一般約 0.25 秒；2026-10-14 RODE 錄音句間 0.56～1.04 秒）
PAUSE_COMMA_SECONDS = 0.5
# 間隔達到此秒數視為句子結束、補「。」（2026-10-14 外接麥克風實測：句間 0.84～0.94 秒，句中多為 0.5～0.72 秒）
PAUSE_SENTENCE_SECONDS = 0.85
# 問句起點：whole＝上一個句號之後的整句；shortest＝最短、仍判為問句的結尾小句組（2026-10-14 前的做法）
QUESTION_START = "whole"


def bare(text: str) -> str:
    return _PUNCT.sub("", text)


def insert_pause_commas(tokens: list[str], timestamps: list[float], threshold: float = PAUSE_COMMA_SECONDS,
                        sentence: float = PAUSE_SENTENCE_SECONDS) -> str:
    """依 ASR 每個字的開始時間，在停頓處補「，」，長停頓補「。」（字數與時間數不一致時不補）。"""
    if len(tokens) != len(timestamps):
        return "".join(tokens)
    out = []
    for i, token in enumerate(tokens):
        out.append(token)
        if i + 1 < len(tokens):
            gap = timestamps[i + 1] - timestamps[i]
            if gap >= sentence:
                out.append("。")
            elif gap >= threshold:
                out.append("，")
    return "".join(out)


def is_argument(clause: str, prev: str = "") -> bool:
    c = TAIL.sub("", bare(clause))
    joined = bare(prev) + c
    return bool(ARGUE_WHAT.search(c) or ARGUE_TAG.search(c) or ARGUE_TAG.search(joined) or RHETORICAL.search(joined))


def rule_question(text: str, allow_start: bool = True) -> bool:
    """只看文字判斷是否為問句。allow_start=False：「請問」只標記問句起點，不單獨成為問句。"""
    c = TAIL.sub("", bare(text))
    if not c or is_argument(c):
        return False
    if ASK_OPINION.search(c) or (allow_start and ASK_START.search(c)) or TAIL_NOT.search(c):
        return True
    has_wh = bool(WH.search(c)) and not NOT_WH.search(c)
    if c.endswith("嗎") and len(c) > 3:  # 「這個嗎，……」為停頓（簡編本「嗎」義項二）
        return True
    if INDIRECT.search(c):
        return False
    if c.endswith("呢"):  # 只有「呢」是加強、確定語氣（簡編本「呢」義項二）
        return has_wh or bool(AXNA.search(c)) or "還是" in c or bool(IF_NE.search(c))
    # 「吧」不單獨作為問句依據（簡編本「吧」五個義項都不是疑問）
    return has_wh or bool(AXNA.search(c)) or bool(CHOICE.search(c))


def _punct_rejects(text: str) -> bool:
    """標點模型加了問號，但屬陳述語氣。"""
    c = TAIL.sub("", bare(text))
    if is_argument(c):
        return True
    if ASK_OPINION.search(c) or IF_NE.search(c):
        return False
    if INDIRECT.search(c) and not c.endswith("嗎"):
        return True
    if c.endswith(("呢", "吧")) and not (WH.search(c) or AXNA.search(c) or "還是" in c):
        return True
    return bool(NOT_WH.search(c)) and not AXNA.search(c)


@dataclass
class Part:
    is_question: bool
    text: str      # 含標點，問句以「？」結尾
    start: int     # 在去標點文字中的位置（推算音訊位置用）
    end: int


def _marks(text: str, punctuate: Callable[[str], str]) -> tuple[str, list[str]]:
    """（去標點文字, 每個字之後的標點）。

    標點模型的標點優先；模型沒加標點的停頓處補上文字裡的停頓標點；長停頓的「。」蓋過模型的逗號。
    """
    plain = bare(text)
    pauses: dict[int, str] = {}
    seen = 0
    for ch in text:
        if _PUNCT.fullmatch(ch):
            if seen and ch.strip():
                pauses[seen - 1] = "。" if ch in "。．." else "，"
        else:
            seen += 1
    marks = [""] * len(plain)
    seen = 0
    for ch in punctuate(plain):
        if _PUNCT.fullmatch(ch):
            if seen and ch.strip():
                marks[seen - 1] = ch
        elif seen < len(plain):
            seen += 1
    for i, mark in pauses.items():
        if i < len(marks) and (not marks[i] or (mark == "。" and marks[i] not in _TERMINAL)):
            marks[i] = mark
    return plain, marks


def split_questions(text: str, punctuate: Callable[[str], str], after_question: bool = False) -> list[Part]:
    """把一段文字依序切成陳述段與問句（涵蓋全部文字）。

    after_question：這段話緊接在一個問句之後；開頭是「還是…」時算選擇問的後半（晚上可以看電視嗎／還是要早點關燈）。
    """
    plain, marks = _marks(text, punctuate)
    if not plain:
        return []
    start = 0
    for i, m in enumerate(marks):
        if not m:
            continue
        if m in "，,、；;":
            start = i + 1
        else:
            if m in "。．." and i + 1 < len(plain) and UNFINISHED.search(TAIL.sub("", plain[start:i + 1])):
                marks[i] = "，"
            else:
                start = i + 1
    # 小句：(開始, 結束, 結尾標點)
    clauses, start = [], 0
    for i, m in enumerate(marks):
        if m or i == len(plain) - 1:
            clauses.append((start, i + 1, m))
            start = i + 1
    # 句：連續小句，直到句號、問號、驚嘆號
    sentences, current = [], []
    for c in clauses:
        current.append(c)
        if c[2] and c[2] in _TERMINAL:
            sentences.append(current)
            current = []
    if current:
        sentences.append(current)

    # 每個問句涵蓋的小句（全域編號：開始 → 結束）
    question_at: dict[int, int] = {}
    base = 0
    prev_question = after_question
    for n, sent in enumerate(sentences):
        texts = [plain[a:b] for a, b, _ in sent]
        following = sentences[n + 1] if n + 1 < len(sentences) else None
        ranges = _question_ranges(texts, sent[-1][2], "".join(texts), following, plain, prev_question)
        for k0, k1 in ranges:
            question_at[base + k0] = base + k1
        prev_question = bool(ranges) and ranges[-1][1] == len(texts) - 1
        base += len(sent)

    parts: list[Part] = []
    i = 0
    while i < len(clauses):
        if i in question_at:
            j = question_at[i]
            body = "".join(plain[a:b] + m for a, b, m in clauses[i:j]) + plain[clauses[j][0]:clauses[j][1]]
            parts.append(Part(True, body.rstrip("，,、；;") + "？", clauses[i][0], clauses[j][1]))
            i = j + 1
            continue
        # 陳述各自保留意圖（question-window 第 1 節第 2 點）：遇到句號、驚嘆號就分開
        j = i
        while j + 1 < len(clauses) and j + 1 not in question_at and not (clauses[j][2] and clauses[j][2] in _TERMINAL):
            j += 1
        body = "".join(plain[a:b] + m for a, b, m in clauses[i:j + 1]).rstrip("，,、；;")
        # 陳述段不留問號：句中改成逗號；句尾的反問、爭吵改成驚嘆號（標點手冊：強烈的反詰疑問句用驚嘆號）
        body = re.sub(r"[？?](?=.)", "，", body)
        if body.endswith(("？", "?")):
            body = body[:-1] + ("！" if is_argument(body) else "。")
        parts.append(Part(False, body, clauses[i][0], clauses[j][1]))
        i = j + 1
    parts = [p for p in parts if bare(p.text)]
    # 只有語氣詞或 1～2 個字的片段（「啊，對了，…」的「啊」、稱呼「阿公」、斷掉的「他」）併到下一段
    merged: list[Part] = []
    for p in parts:
        if merged and not merged[-1].is_question and len(TAIL.sub("", bare(merged[-1].text))) <= 2:
            lead = merged.pop()
            p = Part(p.is_question, lead.text.rstrip("。！") + "，" + p.text, lead.start, p.end)
        merged.append(p)
    return merged


def _question_ranges(texts: list[str], mark: str, joined: str, next_sentence, plain: str,
                     after_question: bool = False) -> list[tuple[int, int]]:
    """一句中問句所在的小句範圍（含頭尾編號）。

    逐小句判斷，並看前後小句：和後一小句合起來是非疑問用法（有什麼不舒服，就按鈴）、和前一小句合起來是間接問句
    （去問護理站，有沒有手套）就不算。最後一個小句另以「剩下的小句合起來」判斷（飯前吃，還是飯後吃）。
    """
    is_qmark = bool(mark) and mark in "？?"
    if is_qmark and next_sentence is not None:
        # 標點模型可能把「我怎麼勸？她都不肯吃藥」切開：非疑問用法跨問號檢查
        ahead = plain[next_sentence[0][0]:next_sentence[-1][1]][:8]
        if NOT_WH.search(bare(joined) + ahead) and not NOT_WH.search(bare(joined)):
            return []
    ranges, last = [], 0
    for i, t in enumerate(texts):
        prev = texts[i - 1] if i else ""
        nxt = texts[i + 1] if i + 1 < len(texts) else ""
        if bare(nxt) in ("沒", "沒有") and re.search(r"(了|過)$", bare(t)):
            continue  # 標點模型把「吃藥了沒」切成「吃藥了，沒」：交給下一個小句一起判斷
        q = (rule_question(t, allow_start=False) and not is_argument(t, prev)
             and not NOT_WH.search(bare(t + nxt)) and not INDIRECT.search(bare(prev + t)))
        if not q and bare(t) in ("沒", "沒有") and re.search(r"(了|過)$", bare(prev)):
            q = True
        if i == len(texts) - 1 and not q:
            rest = "".join(texts[last:])
            argued = any(is_argument(texts[k], texts[k - 1] if k else "") for k in range(last, len(texts)))
            # 緊接在問句之後的「還是…」是選擇問的後半（晚上可以看電視嗎，還是要早點關燈）
            follows = (last > 0 or after_question) and bare(rest).startswith("還是")
            q = not argued and (rule_question(rest) or follows or (is_qmark and not _punct_rejects(rest)))
        if q:
            if (last > 0 or after_question) and bare(texts[last]).startswith("還是"):
                start = 0
            else:
                start = _question_start(texts[last:i + 1])
            ranges.append((last + start, i))
            last = i + 1
    return ranges


def _question_start(texts: list[str]) -> int:
    """問句從第幾個小句開始：「請問」之後整段；否則取最短、仍判為問句的結尾小句組（「還是」開頭要連同前一小句）。"""
    starts = [k for k, t in enumerate(texts) if ASK_START.search(t)]
    if starts:
        return starts[-1]
    if QUESTION_START == "whole":
        return 0
    k = len(texts) - 1
    for j in range(len(texts) - 1, -1, -1):
        suffix = "".join(texts[j:])
        if rule_question(suffix) and not bare(suffix).startswith("還是"):
            k = j
            break
    extended = 0
    while k > 0 and extended < MAX_EXTEND:
        prev = TAIL.sub("", bare(texts[k - 1]))
        if rule_question(prev, allow_start=False) or is_argument(prev):
            break
        if not (NO_SUBJECT.match(bare(texts[k])) or DANGLING.search(prev)):
            break
        k -= 1
        extended += 1
    while k > 0 and LEAD.match(TAIL.sub("", bare(texts[k - 1]))):
        k -= 1
    return k
