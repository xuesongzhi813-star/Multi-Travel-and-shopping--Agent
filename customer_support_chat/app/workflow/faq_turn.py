"""Identify policy clauses and project them onto public catalog vocabulary."""

from dataclasses import dataclass
import re
from typing import Literal
from unicodedata import normalize

from faq_extension.contracts import FaqCategory, FaqSearchResult, PublicFaqQuestion


@dataclass(frozen=True)
class FaqTurn:
    kind: Literal["ordinary", "standalone", "mixed", "rephrase"]
    question: PublicFaqQuestion | None = None
    business_text: str = ""
    local_status: Literal["no_evidence", "rephrase"] = "rephrase"


_CLAUSE = re.compile(r"[,，。；;！？?\n]+|顺便(?:问一下|问)?|另外(?:问一下|问)?|并(?:想问|问一下|问)")
_INTENT = re.compile(r"政策|规定|怎么办|如何|怎么|哪里|哪儿|是否|吗|能否|可以|多少|收费|准则")
_NEGATED = re.compile(r"(?:不用|别|不要|不想)(?:再)?(?:查|问|解释|了解|提)")
_PRIVATE = re.compile(
    r"(?<![A-Z0-9-])DEMO-[A-Z][0-9]{1,3}(?![A-Z0-9-])|订单(?:号|编号)|姓名|身份证|证件号|护照|电话|手机号|邮箱|"
    r"[\w.+-]+@[\w.-]+|\b\d{5,}\b|\b[A-Z]{2,}\d{3,}\b", re.IGNORECASE,
)
_TOPICS: tuple[tuple[FaqCategory, str, str], ...] = (
    ("baggage", r"行李.{0,6}(?:延误|没收到|没拿到|未到|丢失|找不到)|(?:没拿到|没收到|未收到).{0,5}行李", "行李延误 托运行李未收到"),
    ("baggage", r"行李.{0,5}(?:破损|受损|损坏)|(?:破损|受损|损坏).{0,4}行李", "行李受损 托运行李损坏"),
    ("baggage", r"行李.{0,7}(?:额度|托运|限额|件数)|(?:托运|随身).{0,5}行李", "托运行李 行李额度"),
    ("changes_refunds", r"退票|退款", "航班取消 退款 退票"),
    ("changes_refunds", r"改签|改订|改票|变更机票", "航班改签 改订机票"),
    ("disruptions", r"航班.{0,5}(?:取消|延误)|航变|替代航班", "航班取消 替代航班"),
    ("lodging", r"酒店.{0,6}(?:设施|位置|政策|目录|取消)|住宿.{0,6}(?:设施|位置|政策|目录)|(?:机场酒店|市区酒店).{0,5}(?:比较|核实|参考)", "苏黎世酒店 住宿设施 官方目录"),
)
_PUBLIC_TERMS = ("SWISS", "ZRH", "BASIC", "LIGHT", "COMFORT")
_UNSUPPORTED_SCOPE = re.compile(
    r"航班.{0,4}延误|国航|中国国际航空|南航|东航|海航|川航|厦航|深航|汉莎|"
    r"法航|英航|达美|美联航|新加坡航空|国泰|air china|lufthansa", re.IGNORECASE,
)
_PUBLIC_CONTEXT = re.compile(r"航班.{0,4}(?:取消|延误)|替代航班|没有合适的航班|找不到合适的航班")
_REFUND_SCOPE = re.compile(r"(?:没有|找不到|无).{0,12}(?:替代航班|合适的航班)")


def _policy_topic(clause: str) -> tuple[FaqCategory, str] | None:
    if not _INTENT.search(clause) or _NEGATED.search(clause):
        return None
    for category, pattern, text in _TOPICS:
        if re.search(pattern, clause):
            return category, text
    return None


def classify_faq_turn(content: str) -> FaqTurn:
    clauses = [piece.strip() for piece in _CLAUSE.split(normalize("NFKC", content)) if piece.strip()]
    policy = [(index, topic) for index, clause in enumerate(clauses) if (topic := _policy_topic(clause))]
    if not policy:
        return FaqTurn("ordinary", business_text=content)
    policy_indices = {index for index, _ in policy}
    index, (category, topic) = policy[0]
    policy_clause = clauses[index]
    if len(policy) == 1 and index and _PUBLIC_CONTEXT.search(clauses[index - 1]) and not _PRIVATE.search(clauses[index - 1]):
        policy_clause = clauses[index - 1] + " " + policy_clause
        policy_indices.add(index - 1)
    business = "，".join(clause for number, clause in enumerate(clauses) if number not in policy_indices)
    if len(policy) != 1 or _PRIVATE.search(policy_clause):
        return FaqTurn("rephrase", business_text=business)
    if _UNSUPPORTED_SCOPE.search(policy_clause):
        return FaqTurn("rephrase", business_text=business, local_status="no_evidence")
    if category == "changes_refunds" and re.search(r"退款|退票", policy_clause) and _REFUND_SCOPE.search(policy_clause):
        topic = "航班取消后没有合适的替代航班，在哪里查看退款选项？"
    public_terms = [
        term for term in _PUBLIC_TERMS
        if re.search(rf"(?<![A-Z0-9]){term}(?![A-Z0-9])", policy_clause.upper())
    ]
    question = PublicFaqQuestion(text=" ".join([topic, *public_terms]), category=category)
    return FaqTurn("mixed" if business else "standalone", question, business)


def faq_reply(result: FaqSearchResult) -> str:
    if result.status == "ok":
        citation = result.citations[0]
        return f"{citation.text}\n适用条件：{citation.applicability}"
    return {
        "no_evidence": "现有公开资料不足以核实这项政策。",
        "not_published": "政策检索暂未发布。",
        "unavailable": "政策检索暂不可用，请稍后再试。",
        "rephrase": "请单独重述公共政策问题，省略姓名、联系方式和订单号等个人信息。",
    }[result.status]
