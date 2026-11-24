OBJECT_TYPES = ["fact", "concept", "idea", "project", "decision", "experience", "procedure", "instruction",
                "preference", "research", "reference", "document", "observation", "hypothesis", "question", "entity"]

# type -> (label, default importance)
EDGE_TYPES = {
    "related_to": {"symmetric": True, "inverse": "related_to", "weight": 0.5},
    "part_of": {"symmetric": False, "inverse": "has_part", "weight": 0.75},
    "depends_on": {"symmetric": False, "inverse": "required_by", "weight": 0.85},
    "supports": {"symmetric": False, "inverse": "supported_by", "weight": 0.8},
    "contradicts": {"symmetric": True, "inverse": "contradicts", "weight": 0.9},
    "derived_from": {"symmetric": False, "inverse": "source_of", "weight": 0.7},
    "references": {"symmetric": False, "inverse": "referenced_by", "weight": 0.6},
    "explains": {"symmetric": False, "inverse": "explained_by", "weight": 0.8},
    "implements": {"symmetric": False, "inverse": "implemented_by", "weight": 0.85},
    "updates": {"symmetric": False, "inverse": "updated_by", "weight": 0.85},
    "replaces": {"symmetric": False, "inverse": "replaced_by", "weight": 0.85},
    "similar_to": {"symmetric": True, "inverse": "similar_to", "weight": 0.45},
}

VERIFICATION = ["ai_extracted", "unverified", "user_verified", "disputed"]
