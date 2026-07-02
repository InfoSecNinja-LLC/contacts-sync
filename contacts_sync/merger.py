def merge_single_value(current_value, current_updated_at, incoming_value, incoming_updated_at):
    if not incoming_value or incoming_value == current_value:
        return current_value, current_updated_at
    if not current_updated_at or incoming_updated_at >= current_updated_at:
        return incoming_value, incoming_updated_at
    return current_value, current_updated_at


def merge_multi_value(current_values, incoming_values, normalize=lambda v: v):
    seen = {}
    for value in current_values:
        seen[normalize(value)] = value
    for value in incoming_values:
        key = normalize(value)
        if key not in seen:
            seen[key] = value
    return list(seen.values())
