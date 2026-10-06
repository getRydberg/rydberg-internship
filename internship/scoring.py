"""Validated YAML profiles and deterministic explainable scoring."""
import math
import re

import yaml

from .parser import FLAGS

# Source frequently omits the country for Canadian cities/provinces.
CANADA = re.compile(r"\b(canada|toronto|vancouver|montreal|montréal|ottawa|waterloo|"
                    r"kitchener|calgary|edmonton|quebec|québec|winnipeg|halifax|"
                    r"ontario|british columbia|alberta|saskatchewan|manitoba)\b|"
                    r",\s*(ON|BC|AB|QC|NS|NB|NL|PE|SK|MB|YT|NT|NU)\b", re.I)


def matches(term, text):
    return re.search(r"(?<!\w)" + re.escape(term.casefold()) + r"(?!\w)", text.casefold()) is not None


def load_profile(raw):
    if len(raw.encode()) > 32768:
        raise ValueError("Profile exceeds 32 KiB")
    try:
        p = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ValueError("Invalid YAML") from exc
    if not isinstance(p, dict):
        raise ValueError("Profile must be a mapping")
    allowed = {"terms", "companies", "locations", "exclude_terms", "min_score",
               "require_flags_absent", "company_boost", "location_boost", "notifications"}
    if set(p) - allowed:
        raise ValueError("Unknown profile fields")
    def strings(value):
        return isinstance(value, list) and len(value) <= 200 and all(isinstance(x, str) and 0 < len(x) <= 200 for x in value)
    def number(value):
        return type(value) in (int, float) and math.isfinite(value) and abs(value) <= 10000
    terms = p.setdefault("terms", {})
    if not isinstance(terms, dict) or len(terms) > 200 or not all(isinstance(k, str) and 0 < len(k) <= 200 and number(v) for k, v in terms.items()):
        raise ValueError("terms must map keywords to finite numeric weights")
    for field, default in (("min_score", 4), ("company_boost", 2), ("location_boost", 1)):
        if not number(p.setdefault(field, default)):
            raise ValueError(f"Invalid {field}")
    for field in ("exclude_terms", "require_flags_absent"):
        if not strings(p.setdefault(field, [])):
            raise ValueError(f"Invalid {field}")
    if set(p["require_flags_absent"]) - set(FLAGS.values()):
        raise ValueError("Unknown eligibility flag")
    for field, keys in (("companies", ("prefer",)), ("locations", ("prefer", "exclude_countries"))):
        obj = p.setdefault(field, {})
        if not isinstance(obj, dict) or set(obj) - set(keys):
            raise ValueError(f"Invalid {field}")
        for key in keys:
            if not strings(obj.setdefault(key, [])):
                raise ValueError(f"Invalid {field}.{key}")
    n = p.setdefault("notifications", {})
    if not isinstance(n, dict) or set(n) - {"mode", "backends"}:
        raise ValueError("Invalid notifications")
    if n.setdefault("mode", "immediate") not in ("immediate", "digest"):
        raise ValueError("Notification mode must be immediate or digest")
    if not strings(n.setdefault("backends", [])) or set(n["backends"]) - {"ntfy", "smtp", "webhook"}:
        raise ValueError("Unknown notification backend")
    return p


def score_role(role, profile, resume_text=''):
    text = role["role"] + " " + role["section"]
    excluded = [term for term in profile["exclude_terms"] if matches(term, text)]
    excluded += [flag for flag in profile["require_flags_absent"] if role["flags"].get(flag)]
    # A mixed-country posting remains eligible if at least one listed location is allowed.
    def allowed(location):
        return not any((bool(CANADA.search(location)) if c.casefold() == "canada" else matches(c, location))
                       for c in profile["locations"]["exclude_countries"])
    locations = [loc for loc in role["locations"] if allowed(loc)]
    if role["locations"] and not locations:
        excluded.append("excluded country")
    matched = [term for term in profile["terms"] if matches(term, text)]
    score = sum(profile["terms"][term] for term in matched)
    if resume_text:
        from .resume import skill_terms
        resume_matches = [term for term in skill_terms(resume_text) if matches(term, text)]
        score += 2 * len(resume_matches)
        matched.extend('Resume: ' + term for term in resume_matches)
    if role["company"].casefold() in {c.casefold() for c in profile["companies"]["prefer"]}:
        score += profile["company_boost"]
        matched.append(role["company"])
    for loc in dict.fromkeys(profile["locations"]["prefer"]):
        if any(matches(loc, actual) for actual in locations):
            score += profile["location_boost"]
            matched.append(loc)
    return {"score": score, "matched": matched, "eligible": not excluded, "excluded": excluded}
