DAT_TO_INTERNAL = {
    "Van": "53_dry_van",
    "Reefer": "reefer",
    "Flatbed": "flatbed",
    "Step Deck": "step_deck",
    "Power Only": "power_only",
    "Hotshot": "hotshot",
}

INTERNAL_TO_DAT = {v: k for k, v in DAT_TO_INTERNAL.items()}

def equipment_matches(carrier_eq: str, load_eq: str) -> bool:
    """
    Checks if carrier equipment matches load equipment using bidirectional mapping.
    """
    if not carrier_eq or not load_eq:
        return False
        
    c_norm = DAT_TO_INTERNAL.get(carrier_eq, carrier_eq).lower()
    l_norm = DAT_TO_INTERNAL.get(load_eq, load_eq).lower()
    
    return c_norm == l_norm or c_norm in l_norm or l_norm in c_norm
