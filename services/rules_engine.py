from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple
import math
import re

INGREDIENTS = {
    "ALOE": ["aloe", "aloe vera"],
    "ARNICA": ["arnica", "arnica montana"],
    "CAMPHOR": ["camphor"],
    "CAPSAICIN": ["capsaicin"],
    "CBD": ["cbd", "cannabidiol"],
    "DICLOFENAC SODIUM": ["diclofenac sodium", "diclofenac"],
    "HEMP": ["hemp", "hemp seed oil", "hemp extract"],
    "HOMEOPATHIC": ["homeopathic", "homeopathy"],
    "LIDOCAINE": ["lidocaine"],
    "MENTHOL": ["menthol"],
    "METHYL SALICYLATE": ["methyl salicylate", "wintergreen oil"],
    "TROLAMINE SALICYLATE": ["trolamine salicylate"],
}

SUBCATEGORY_TERMS = {
    "ARTHRITIS": ["arthritis", "arthritis pain", "rheumatoid arthritis", "osteoarthritis", "joint pain due to arthritis"],
    "MENSTRUAL": ["menstrual pain", "menstrual cramps", "period pain", "period cramps", "menstrual relief", "pms pain", "dysmenorrhea"],
    "MIGRAINE&HEADACHE": ["headache", "migraine", "tension headache", "head pain", "sinus headache"],
}
MULTI_TERMS = ["multi-symptom", "multiple pain", "arthritis and muscle pain", "joint and muscle pain",
               "back and body pain", "headache and migraine", "menstrual and back pain",
               "aches and pains", "whole body pain relief", "all-in-one pain relief"]

ALLOWED_TOPICAL_FORMS = {"cream","gel","spray","lotion","patch","ointment","balm","roll-on","roll on","stick","oil","foam","liquid"}
ORAL_FORMS = {"tablet","tablets","caplet","caplets","capsule","capsules","soft gel","soft gels","pellet","pellets","gelcap","gelcaps"}

SCENT_MAP = {
    "BERGAMOT":["bergamot"], "CITRUS":["citrus","lemon"], "COCONUT":["coconut"],
    "FRESH":["fresh scent","fresh"], "LAVENDER":["lavender"], "LIGHT":["light scent","light fragrance","mild scent"],
    "MENTHOL":["menthol scent","menthol fragrance"], "MINT":["mint","peppermint","spearmint"],
    "VANILLA":["vanilla"], "VANISHING":["vanishing scent","vanishing fragrance"]
}
STRENGTH_MAP = {
    "DOUBLE STRENGTH":["double strength","2x strength","twice the strength"],
    "EXTRA STRENGTH":["extra strength"],
    "HIGH POTENCY":["high potency","potent formula","high strength formula"],
    "MAXIMUM STRENGTH":["maximum strength","max strength","maximum potency"],
}

def clean(v):
    if v is None or (isinstance(v,float) and math.isnan(v)): return ""
    return str(v).strip()

def row_text(row: Dict[str,Any]) -> str:
    vals = []
    for k,v in row.items():
        if v is not None and clean(v) and clean(v).lower() != "nan":
            vals.append(f"{k}: {clean(v)}")
    return " | ".join(vals)

def contains_any(text: str, terms: List[str]) -> bool:
    t = text.lower()
    return any(x.lower() in t for x in terms)

def get_source_text(row: Dict[str,Any], image_ocr: str = "") -> str:
    wanted = [
        "ITEM_DESCRIPTION","ITEM DESCRIPTION","PRO ITEM DESCRIPTION ","PRO ITEM DESCRIPTION",
        "COMMON CONSUMER NAME ","COMMON CONSUMER NAME","COMPETITIVE CATEGORY OGRDS ","COMPETITIVE CATEGORY OGRDS",
        "COMMODITY GROUP ","COMMODITY GROUP","DRUG FACT PURPOSE","DRUG FACT ACTIVE INGREDIENT",
        "BC CONCAT DRUG FACT ACTIVE INGREDIENT ","BC CONCAT DRUG FACT ACTIVE INGREDIENT",
        "FORM","BC CONCAT FORM ","BC CONCAT FORM","FORMULATION","STRATEGIC INGREDIENT PRESENCE CLAIM",
        "IH INGREDIENT 1","IH INGREDIENT 2","IH INGREDIENT 3","SCENT","STRENGTH CLAIM","TARGET GROUP AGE",
        "BRAND","RB_BRAND","RB_SUBBRAND","RB_MANUFACTURER","CLAIM","COMPARE TO CLAIM"
    ]
    parts = [clean(row.get(c,"")) for c in wanted if clean(row.get(c,""))]
    if image_ocr:
        parts.append(image_ocr)
    return " ".join(parts)

def ingredient_presence(row, image_ocr=""):
    text = get_source_text(row, image_ocr).lower()
    found = {}
    for name, terms in INGREDIENTS.items():
        found[name] = contains_any(text, terms)
    return found

def classify_ingredient(row, image_ocr=""):
    found = ingredient_presence(row, image_ocr)
    n = sum(found.values())
    rb = "NOT STATED" if n == 0 else ["", "SINGLE","DUAL","THREE","FOUR","FIVE"][min(n,5)]
    return found, rb, min(n,6)

def classify_scent(row, image_ocr=""):
    text = get_source_text(row, image_ocr).lower()
    raw = clean(row.get("SCENT",""))
    if any(x in text for x in ["odorless","unscented","non-fragrance","fragrance-free","no scent","scent free"]):
        return "UNSCENTED"
    for val, terms in SCENT_MAP.items():
        if contains_any(text, terms):
            return val
    if raw:
        return "OTHER SCENT"
    return "UNSCENTED"

def classify_strength(row, image_ocr=""):
    text = get_source_text(row, image_ocr).lower()
    for val, terms in STRENGTH_MAP.items():
        if contains_any(text, terms):
            return val
    return "REGULAR STRENGTH"

def classify_time(row, image_ocr=""):
    text = get_source_text(row, image_ocr).lower()
    return "NIGHTTIME" if any(x in text for x in ["overnight","bedtime","at night","while sleeping","nighttime","night"]) else "DAYTIME"

def classify_subcategory(row, image_ocr=""):
    text = get_source_text(row, image_ocr).lower()
    if contains_any(text, MULTI_TERMS):
        return "MULTI-SYMPTOM"
    condition_hits = 0
    if contains_any(text, ["arthritis","joint pain","joint discomfort"]): condition_hits += 1
    if contains_any(text, ["muscle pain","muscle","body pain","back pain"]): condition_hits += 1
    if contains_any(text, SUBCATEGORY_TERMS["MENSTRUAL"]): condition_hits += 1
    if contains_any(text, SUBCATEGORY_TERMS["MIGRAINE&HEADACHE"]): condition_hits += 1
    if condition_hits >= 2:
        return "MULTI-SYMPTOM"
    matches = [k for k,terms in SUBCATEGORY_TERMS.items() if contains_any(text,terms)]
    if len(matches)==1: return matches[0]
    if len(matches)>1: return "MULTI-SYMPTOM"
    return clean(row.get("RB_SUBCATEGORY","")) or "MULTI-SYMPTOM"

def normalize_form(v):
    s=clean(v).lower().replace("_"," ")
    return s

def classify_form(row, image_ocr=""):
    text=get_source_text(row,image_ocr).lower()
    raw=normalize_form(row.get("FORM",""))
    for target, terms in {
        "ROLL ON":["roll on","roll-on","rollerball"],
        "CREAM":["cream","creme"],
        "PATCH":["patch","gel sheet"],
        "GEL":["gel"],
        "SPRAY":["spray"],
        "FOAM":["foam"],
    }.items():
        if any(t in text for t in terms): return target
    return raw.upper() if raw else "AO FORM"

def classify_container(row, image_ocr=""):
    text=get_source_text(row,image_ocr).lower()
    for val, terms in {
        "BOTTLE":["bottle"],"BOX":["box","molded tray"],"CAN":["can"],"CARDED":["carded"],
        "ENVELOPE":["envelope"],"JAR":["jar"],"POUCH":["pouch","bag"],"TIN":["tin"],
        "TUB":["tub"],"TUBE":["tube","stick in box"],"WRAP":["wrap"]
    }.items():
        if any(t in text for t in terms): return val
    raw=clean(row.get("PACKAGE GENERAL SHAPE",""))
    return raw.upper() if raw else "BOTTLE"

def classify_scope(row, guide_context="", image_ocr="") -> Tuple[str,str,int]:
    cc=clean(row.get("COMPETITIVE CATEGORY OGRDS ","")) or clean(row.get("COMPETITIVE CATEGORY OGRDS",""))
    ccn=clean(row.get("COMMON CONSUMER NAME ","")) or clean(row.get("COMMON CONSUMER NAME",""))
    commodity=clean(row.get("COMMODITY GROUP ","")) or clean(row.get("COMMODITY GROUP",""))
    form=normalize_form(row.get("FORM",""))
    text=get_source_text(row,image_ocr).lower()
    reasons=[]

    # Global exclusions from DB Guide.
    if "first aid" in text or "first aid" in commodity.lower():
        return "NOT INCLUDED","FIRST AID is excluded by the DB Guide.",100
    if any(x in text for x in ["microwaveable","electric","usb","plug-in","plug in","heating pad"]):
        if "patch" not in text or "medicated" not in text:
            return "NOT INCLUDED","Electric/microwave/heating-pad type is excluded by the DB Guide.",100
    if form in ORAL_FORMS or any(x in text for x in ORAL_FORMS):
        return "NOT INCLUDED","Oral/swallowable form is excluded by the DB Guide.",100

    comm=commodity.upper()
    cat=cc.upper()
    ccn_u=ccn.upper()
    if "HAND & BODY LOTION-ADULT-CT" in comm:
        ok=("HAND & BODY LOTION-ADULT-CT" in cat and "HAND AND BODY AND SKIN PRODUCT" in ccn_u and "RE THINK" in text.upper())
        return ("INCLUDED" if ok else "NOT INCLUDED",
                "Hand/body lotion count rule: category + CCN + RE THINK brand required.",100)
    if "HAND & BODY LOTION-ADULT-FL OZ" in comm:
        ok=("HAND & BODY LOTION-ADULT-FL OZ" in cat and "HAND AND BODY AND SKIN PRODUCT" in ccn_u and "SOCIAL CBD" in text.upper())
        return ("INCLUDED" if ok else "NOT INCLUDED",
                "Hand/body lotion liquid rule: category + CCN + SOCIAL CBD required.",100)
    if "EXTERNAL PAIN REMEDY" in cat or "ANALGESIC AND CHEST RUB" in commodity.upper():
        f=classify_form(row,image_ocr).lower()
        ok=f in {x.lower() for x in ALLOWED_TOPICAL_FORMS} or f=="ao form"
        return ("INCLUDED" if ok else "NOT INCLUDED",
                "External pain remedy requires an approved topical form.",90 if ok else 100)
    if "ICE & HEAT PACK" in cat or "ICE AND HEAT PACK" in commodity.upper():
        bad=any(x in text for x in ["electric","usb","plug-in","microwave","heating pad"])
        medicated_patch=("medicated patch" in text or ("patch" in text and "pain" in text))
        ok=not bad and medicated_patch
        return ("INCLUDED" if ok else "NOT INCLUDED",
                "Ice/heat rule: exclude devices/first-aid therapy; include qualifying medicated pain-relief patches.",80 if "patch" in text else 100)
    if "HEADACHE BDY PN RMDY" in cat:
        ok=not any(x in text for x in ORAL_FORMS)
        return ("INCLUDED" if ok else "NOT INCLUDED",
                "Headache/body pain rule requires approved non-oral form.",100)
    # Conservative fallback: use guide/RAG/LLM for ambiguous cases.
    if any(x in text for x in ["pain","analgesic","arthritis","migraine","headache","menstrual","muscle","joint"]):
        return "REVIEW","Scope appears potentially relevant but needs guide/AI confirmation.",70
    return "NOT INCLUDED","No evidence that the item satisfies the Topical Analgesics scope.",100

def apply_deterministic(row: Dict[str,Any], image_ocr="", guide_context=""):
    result={}
    found, rb_ing, numeric_ing = classify_ingredient(row,image_ocr)
    result["3926_RB TOPICAL ANALGESICS"]=numeric_ing
    result["RB_INGREDIENT"]=rb_ing
    for ing,present in found.items():
        result["RB_"+ing]=("TOTAL "+ing) if present else ("NO "+ing)
    result["RB_SCENT"]=classify_scent(row,image_ocr)
    result["RB_STRENGTH"]=classify_strength(row,image_ocr)
    result["RB_TIME OF DAY"]=classify_time(row,image_ocr)
    result["RB_SUBCATEGORY"]=classify_subcategory(row,image_ocr)
    result["RB_FORM"]=classify_form(row,image_ocr)
    result["RB_CONTAINER"]=classify_container(row,image_ocr)
    result["RB_SEGMENT"]=f"{result['RB_FORM']} {result['RB_CONTAINER']}"
    result["RB_SIZE"]=clean(row.get("BASE SIZE","")) or clean(row.get("TOTAL SIZE","")) or clean(row.get("TOTAL SIZE ",""))
    # Preserve supplied size range when available; derive common ranges when possible.
    result["RB_SIZE RANGE"]=clean(row.get("RB_SIZE RANGE","")) or clean(row.get("RB SIZE RANGE",""))
    result["RB_DURATION"]=clean(row.get("RB_DURATION","")) or "NOT STATED"
    result["RB_BUSINESS UNIT"]=clean(row.get("RB_BUSINESS UNIT ","")) or "HEALTH"
    result["RB_MEGA CATEGORY"]=clean(row.get("RB_MEGA CATEGORY ","")) or "RELIEF"
    result["RB_CATEGORY"]=clean(row.get("RB_CATEGORY ","")) or "TOPICAL ANALGESIC"
    return result

def make_reasoning(changes: Dict[str,Any], scope_status: str, scope_reason: str, image_used: bool):
    reasons=[]
    if scope_status=="NOT INCLUDED": reasons.append(scope_reason)
    elif scope_status=="REVIEW": reasons.append(scope_reason)
    if changes:
        reasons.append("Custom characteristics were evaluated against the supplied syndicated fields and DB Guide.")
    if image_used:
        reasons.append("Package imagery was processed with OCR and included as supporting evidence.")
    return " ".join(reasons)
