"""Room name vs. room-type ("definition") quality check.

In Design Master's "Room Information, Part 1" export each room has:
    - a NAME the engineer typed       — the "Number" column  (e.g. "AC CHASE",
                                          "CLASS 1A", "RR 2", "BREAK")
    - a TYPE / definition selected     — the "Name" column    (e.g.
                                          "FBC Retail, Storage rooms",
                                          "Educational-Classroom (ages 9 plus)",
                                          "Bathrooms/toilet-private")

These map onto the parsed report as RoomInfoP1.number (name) and
RoomInfoP1.name (definition) — see hvac_pipeline.py.

This module flags rooms whose typed name does NOT look consistent with the
selected definition, so an engineer can eyeball them. It is deliberately
conservative — "flag for review": anything it cannot *confidently* match is
surfaced (including rooms with no definition selected).

Matching strategy (no external deps):
  1. Map both the name and the definition onto a set of "concepts" via a
     synonym/abbreviation table (bath/rr/restroom → restroom, etc.).
  2. If they share a concept → OK.
  3. Else if they share a meaningful word, or a fuzzy near-match word → OK.
  4. Otherwise → flag.

Public entry: check_rooms(rooms_p1) -> {"checked": int, "flagged": [ ... ]}.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

# ── The approved master list of room definitions (kept for reference and so
#    the vocabulary is documented in one place; matching works against the
#    definition string actually present on each room). ──────────────────────
ROOM_DEFINITIONS = [
    "All other locker rooms", "Animal imaging (MRI/CT/PET)", "Animal operating rooms",
    "Animal postoperative recovery room", "Animal preparation rooms", "Animal procedure room",
    "Animal surgery scrub", "Arenas", "Art classrooms", "Assembly, concentrated (chairs only)",
    "Auditorium seating area", "Auditoriums", "Auto repair rooms",
    "Automotive motor-fuel dispensing stations", "Bank vaults/safe deposit",
    "Banks or bank lobbies", "Banks or lobbies", "Barber", "Barber shops", "Barbershop",
    "Barracks sleeping areas", "Bars, cocktail lounges", "Bathroom", "Bathrooms/toilet-private",
    "Beauty and nail salons", "Beauty salons", "Beauty/styling stations", "Bedroom (1 person)",
    "Bedroom, Master (2 ppl = Nbr+1)", "Bedroom/living room", "Birthing room", "Booking/waiting",
    "Bowling alley (seating)", "Bowling alleys (seating areas)", "Break rooms",
    "Cafeteria, fast food", "Cafeteria/fast-food dining", "Cell", "Cells w/ plumbing fixtures",
    "Cells w/o plumbing fixtures", "Cells with toilet", "chemical", "Class 1 imaging room",
    "Class 1 Imaging rooms", "Classrooms (age 9 plus)", "Classrooms (age 9+)",
    "Classrooms (ages 5 to 8)", "Classrooms (ages 5-8)", "Clean supply", "Coffee stations",
    "Coin-operated dry cleaner", "Coin-operated laundries", "Commercial dry cleaner",
    "Commercial laundry", "Common corridors", "Computer", "Computer (without printing)",
    "Computer lab", "Computer room (no printing)", "Conference rooms", "Conference/meeting",
    "Copy, printing", "Copy, printing rooms", "Copy/print", "Corridors", "Corridors (ages 5+)",
    "Courtrooms", "Darkrooms", "Day care (through age 4)", "Day room", "Daycare (through age 4)",
    "Daycare sickroom", "Dayroom", "Den", "Dental operatory", "Dental treatment", "Dining rooms",
    "Disco/dance floors", "Dormitory sleeping areas", "Dressing rooms", "Dwelling-unit kitchens",
    "ECT procedure room", "Educational science laboratories", "Elec/Mech", "Elevator car",
    "Embalming room", "Environmental services room", "Exam room", "Exam room (vet)",
    "Exam room (veterinary office)", "Freezer and refrigerated spaces (<50F)", "Gambling casinos",
    "Game arcades", "Garages, common for multiple units", "Gen Outpatient-Lab Wrk Rm",
    "General examination room", "Grooming/bathing", "Guard stations",
    "Gym, sports arena (play area)", "Gym, stadium, arena (play area)", "Hall/Circulation",
    "Health club/aerobics room", "Health club/weight rooms", "Hydrotherapy",
    "Ice arenas without combustion engines", "Imaging (Class 1)", "Imaging (MR/CT/PET)",
    "Imaging (MRI/CT/PET)", "Imaging/X-ray", "Imaging/X-ray (Class 1)",
    "IT equipment facilities (occupiable)", "Janitor", "Janitor closets, trash rooms, recycling",
    "Kitchen (cooking)", "Kitchen(300 other)/5ACH cont", "Kitchenettes", "Kitchens",
    "Kitchens (cooking)", "Kitchens-commercial", "Lab/Sterilization", "Large-animal holding",
    "Large-animal holding room", "Laundry", "Laundry (dryer vented direct out)",
    "Laundry (soiled)", "Laundry rooms within dwelling units", "Laundry rooms, central",
    "Lecture classroom", "Lecture hall (fixed seats)", "Legislative chambers", "Libraries",
    "Lobbies", "Lobbies/prefunction", "Locker rooms for athletic, industrial, and health care facilities",
    "Locker rooms, athletic/industrial/healthcare", "Locker/dressing rooms", "Main entry lobbies",
    "Main entry lobby", "Main Living", "Mall common areas",
    "Manufacturing, hazardous materials used", "Manufacturing, no hazardous materials",
    "Meat processing", "Media", "Media center", "Medication room", "Multipurpose assembly",
    "Multiuse assembly", "Multiuse assembly (tables and chairs)", "Museums (childrens)",
    "Museums/galleries", "Music/theater/dance", "Nail salons", "Nail stations", "Necropsy",
    "Occ storage rooms dry materials", "Occ storage rooms liquids or gels", "Occupational therapy",
    "Office", "Office spaces", "Office/admin", "Operating rooms", "Operatory",
    "Other dental treatment", "Other dental treatment areas", "Paint spray booths",
    "Parking garages", "Pet retail/adoption area", "Pet shops (animal areas)",
    "Pharmacy (prep. area)", "Photo studios", "Physical therapeutic pool",
    "Physical therapeutic pool area", "Physical therapy exercise area",
    "Physical therapy individual room", "Places of religious worship", "Platforms", "Post-op recovery",
    "Postoperative recovery", "Postoperative recovery room", "Preparation rooms", "Procedure room",
    "Prosthetics and orthotics room", "Psychiatric consultation room", "Psychiatric examination room",
    "Psychiatric group room", "Psychiatric seclusion room", "PT individual room", "Reception",
    "Reception areas", "Refrigerated warehouses/freezers", "Refrigerating machinery rooms",
    "Repair garages, enclosed parking garages", "Residential kitchens", "Restaurant dining rooms",
    "Room with adult changing station", "Sales", "Sales (except as below)", "Science laboratories",
    "Shampoo area", "Shipping and receiving", "Shipping/receiving", "Shower rooms",
    "Small-animal cage (static)", "Small-animal cage (ventilated)", "Small-animal cage room (static)",
    "Small-animal cage room (ventilated)", "Small-animal-cage room (static cages)",
    "Small-animal-cage room (static)", "Small-animal-cage room (ventilated cages)",
    "Small-animal-cage room (ventilated)", "Smoking lounges", "Soiled holding",
    "Soiled holding (Neg, 6ACH exh)", "Soiled laundry storage rooms", "Sorting, packing, light assembly",
    "Specialty IC exam room", "Spectator areas", "Speech therapy room", "Speech therapy/consult",
    "Sports locker rooms", "Stages, studios", "Storage", "Storage (dry)", "Storage rooms",
    "Storage rooms, chemical", "Storage, pick up", "Supermarkets", "Surgery scrub",
    "Swimming (pool and deck)", "Swimming pools (pool and deck area)", "Telephone closets",
    "Telephone/data entry", "Ticket booths", "Toilet", "Toilet room", "Toilet rooms and bathrooms",
    "Toilets-private", "Toilets-public", "Transportation waiting", "University/college laboratories",
    "Urgent care exam", "Urgent care examination room", "Urgent care observation",
    "Urgent care observation room", "Urgent care treatment", "Urgent care treatment room",
    "Urgent care triage", "Urgent care triage (Neg)", "Urgent care triage room", "Waiting",
    "Warehouses", "Wood/metal shops", "Woodwork shop/classrooms",
]

# ── Concept synonym table. Each concept maps to a set of trigger words that
#    may appear in EITHER a room name or a definition. Short tokens (< 4 chars,
#    e.g. "rr", "jc", "it") are matched only as exact whole words to avoid false
#    positives; longer tokens also match as a prefix/substring. ──────────────
_CONCEPTS: dict[str, set[str]] = {
    "restroom":   {"bath", "baths", "bathroom", "bathrooms", "restroom", "restrooms",
                   "toilet", "toilets", "washroom", "lavatory", "lav", "rr", "wc",
                   "powder", "men", "mens", "women", "womens", "unisex", "shower", "showers"},
    "corridor":   {"corridor", "corridors", "hall", "hallway", "halls", "circulation",
                   "circ", "passage", "breezeway", "walkway", "vestibule"},
    "storage":    {"storage", "store", "stores", "stor", "closet", "closets", "clst",
                   "stockroom", "stock", "warehouse", "warehouses", "whse", "vault"},
    "office":     {"office", "offices", "ofc", "off", "admin", "administration",
                   "administrative", "workroom", "cubicle", "clerical"},
    "classroom":  {"classroom", "classrooms", "class", "lecture", "seminar", "daycare",
                   "preschool", "kindergarten"},
    "conference": {"conference", "conf", "meeting", "mtg", "boardroom", "board"},
    "kitchen":    {"kitchen", "kitchens", "kitchenette", "kitchenettes", "kit", "pantry",
                   "galley", "cooking"},
    "break":      {"break", "breakroom", "breakrooms", "lounge", "lounges", "lunchroom",
                   "coffee"},
    "mechanical": {"mechanical", "mech", "electrical", "elec", "equipment", "equip",
                   "boiler", "chiller", "mep", "switchgear", "refrigerating", "machinery",
                   "utility", "control", "facp", "chase", "riser"},
    "tech":       {"it", "idf", "mdf", "data", "telecom", "telephone", "telephones",
                   "comm", "communications", "server", "servers", "network", "computer",
                   "computers"},
    "lobby":      {"lobby", "lobbies", "reception", "recep", "waiting", "wait", "entry",
                   "entrance", "foyer", "prefunction", "atrium", "greeting", "greeter"},
    "dining":     {"dining", "cafeteria", "cafe", "restaurant", "diner", "foodcourt",
                   "bar", "bars"},
    "exam":       {"exam", "exams", "examination", "treatment", "treat", "procedure",
                   "operatory", "operating", "surgery", "surgical", "recovery", "pacu",
                   "triage", "consult", "consultation", "birthing", "dental"},
    "locker":     {"locker", "lockers", "dressing", "changing"},
    "laundry":    {"laundry", "laundries", "washer", "dryer", "soiled"},
    "janitor":    {"janitor", "janitors", "jan", "jc", "custodial", "custodian",
                   "housekeeping", "mop", "evs", "trash", "recycling"},
    "bedroom":    {"bedroom", "bedrooms", "bdrm", "sleeping", "dormitory", "dorm",
                   "barracks", "berth", "cell", "cells"},
    "living":     {"living", "livingroom", "den", "dayroom", "family", "parlor"},
    "fitness":    {"gym", "gymnasium", "fitness", "weight", "weights", "aerobics",
                   "exercise", "workout"},
    "pool":       {"pool", "pools", "swimming", "swim", "natatorium", "hydrotherapy", "spa"},
    "lab":        {"lab", "labs", "laboratory", "laboratories", "science", "sterilization"},
    "copyprint":  {"copy", "print", "printing", "repro", "reprographics"},
    "retail":     {"retail", "sales", "shop", "shops", "showroom", "supermarket", "market",
                   "boutique"},
    "garage":     {"garage", "garages", "parking", "repair"},
    "pharmacy":   {"pharmacy", "pharm", "medication", "med", "meds", "rx"},
    "elevator":   {"elevator", "elev", "lift"},
    "stair":      {"stair", "stairs", "stairway", "stairwell", "egress"},
    "imaging":    {"imaging", "xray", "mri", "ct", "pet", "radiology", "darkroom"},
    "animal":     {"animal", "animals", "kennel", "cage", "cages", "vet", "veterinary",
                   "grooming", "necropsy", "embalming"},
    "worship":    {"worship", "sanctuary", "chapel", "prayer", "religious"},
    "assembly":   {"assembly", "auditorium", "auditoriums", "theater", "theatre", "arena",
                   "arenas", "stage", "stages", "spectator", "seating", "ballroom",
                   "casino", "arcade", "platform", "platforms", "multipurpose", "multiuse"},
    "salon":      {"barber", "barbershop", "salon", "salons", "beauty", "nail", "hair",
                   "styling", "shampoo"},
    "library":    {"library", "libraries", "media", "reading"},
    "shipping":   {"shipping", "receiving", "dock", "loading"},
    "courtroom":  {"courtroom", "courtrooms", "court", "legislative", "chambers", "guard",
                   "booking"},
    "therapy":    {"therapy", "therapeutic", "rehab", "occupational", "prosthetics",
                   "orthotics", "psychiatric", "speech"},
    "manufacturing": {"manufacturing", "factory", "assembly", "fabrication", "shop",
                      "woodwork", "metal", "welding", "paint", "spray", "sorting", "packing"},
}

# Words that carry no discriminating meaning — dropped before concept and fuzzy
# matching so they neither trigger a concept nor a spurious fuzzy hit.
_STOPWORDS = {
    "general", "fbc", "imc", "ashrae", "educational", "sports", "other", "others",
    "all", "the", "and", "or", "with", "without", "per", "of", "in", "for", "to",
    "a", "an", "room", "rooms", "area", "areas", "space", "spaces", "ft", "cfm",
    "no", "number", "rm", "rms", "etc", "as", "below", "except", "new", "existing",
    "ppl", "person", "people", "plus", "ages", "age", "main", "common", "private",
    "public", "central", "individual", "facilities", "stations", "station", "deck",
    "pick", "up", "type", "level", "floor", "occ", "cont", "story", "shared",
}


def _normalize(text: str) -> str:
    text = (text or "").lower()
    text = text.replace("&", " and ").replace("/", " ").replace("-", " ")
    text = re.sub(r"\([^)]*\)", " ", text)          # drop parentheticals
    text = re.sub(r"[^a-z0-9 ]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _tokens(text: str) -> list[str]:
    out = []
    for tok in _normalize(text).split():
        if tok in _STOPWORDS:
            continue
        if tok.isdigit():                            # room numbers, ages
            continue
        if len(tok) <= 1:
            continue
        # strip trailing digits off codes like "class1a" -> "class"
        stripped = re.sub(r"\d+[a-z]?$", "", tok)
        out.append(stripped if len(stripped) >= 2 else tok)
    return out


def _concepts(text: str) -> set[str]:
    toks = _tokens(text)
    found = set()
    for concept, keywords in _CONCEPTS.items():
        for kw in keywords:
            for tok in toks:
                if len(kw) < 4:
                    if tok == kw:
                        found.add(concept)
                        break
                else:
                    if tok == kw or tok.startswith(kw) or kw in tok:
                        found.add(concept)
                        break
            else:
                continue
            break
    return found


def _fuzzy_word_match(name_toks: list[str], def_toks: list[str]) -> bool:
    """True if any meaningful name word closely matches a definition word."""
    for a in name_toks:
        if len(a) < 3:
            continue
        for b in def_toks:
            if len(b) < 3:
                continue
            if a == b:
                return True
            if len(a) >= 5 and (a in b or b in a):
                return True
            if SequenceMatcher(None, a, b).ratio() >= 0.86:
                return True
    return False


def classify(name: str, definition: str) -> dict:
    """Classify one room. Returns a dict with:
        status        : one of
                          "ok"                 — name and type agree
                          "mismatch"           — name clearly says something
                                                 different than the type
                          "unverified"         — no recognizable word in the
                                                 name to check against the type
                          "missing_definition" — no room type selected
        name_concepts : sorted list of concepts inferred from the name
        def_concepts  : sorted list of concepts inferred from the definition
        reason        : short human-readable explanation
    """
    definition = (definition or "").strip()
    name_concepts = _concepts(name)

    if not definition:
        return {
            "status": "missing_definition",
            "name_concepts": sorted(name_concepts),
            "def_concepts": [],
            "reason": "No room type selected for this room.",
        }

    def_concepts = _concepts(definition)

    if name_concepts & def_concepts:
        return {"status": "ok", "name_concepts": sorted(name_concepts),
                "def_concepts": sorted(def_concepts), "reason": ""}

    name_toks = _tokens(name)
    def_toks = _tokens(definition)
    if _fuzzy_word_match(name_toks, def_toks):
        return {"status": "ok", "name_concepts": sorted(name_concepts),
                "def_concepts": sorted(def_concepts), "reason": ""}

    # No confident link. Distinguish a real conflict (the name carries a
    # recognizable meaning that differs from the type) from a name we simply
    # can't read (a bare room number / code, an unfamiliar abbreviation).
    if name_concepts:
        status = "mismatch"
        if def_concepts:
            reason = (f"Name looks like {', '.join(sorted(name_concepts))}, "
                      f"but the type reads as {', '.join(sorted(def_concepts))}.")
        else:
            reason = (f"Name looks like {', '.join(sorted(name_concepts))}, "
                      f"which doesn't match the selected type.")
    else:
        status = "unverified"
        if def_concepts:
            reason = (f"No descriptive word in the name to confirm the type "
                      f"({', '.join(sorted(def_concepts))}).")
        else:
            reason = "Couldn't read either the name or the type to compare them."

    return {"status": status, "name_concepts": sorted(name_concepts),
            "def_concepts": sorted(def_concepts), "reason": reason}


def check_rooms(rooms_p1: list[dict]) -> dict:
    """Run the name-vs-definition check over parsed Room Info Part 1 rows.

    `rooms_p1` is the list from report.json: each item has "number" (the room
    NAME) and "name" (the room TYPE / definition).

    Returns {"checked": int, "flagged": [ {name, definition, status, reason,
    name_concepts, def_concepts} ... ]}. Flagged rooms keep the report order.
    """
    flagged = []
    checked = 0
    for r in rooms_p1 or []:
        name = (r.get("number") or "").strip()
        definition = (r.get("name") or "").strip()
        if not name:
            continue
        checked += 1
        result = classify(name, definition)
        if result["status"] != "ok":
            flagged.append({"name": name, "definition": definition, **result})
    return {"checked": checked, "flagged": flagged}
