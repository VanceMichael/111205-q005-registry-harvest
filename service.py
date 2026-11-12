def map_record(raw, aliases):
    mapped = {}
    for target, candidates in aliases.items():
        values = [raw[key] for key in candidates if key in raw and raw[key] not in (None, '')]
        if len(set(map(str, values))) > 1:
            raise ValueError(f"ambiguous value for {target}")
        if values:
            mapped[target] = values[0]
    return mapped
