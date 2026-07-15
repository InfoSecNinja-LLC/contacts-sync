def merge_single_value(current_value, current_updated_at, incoming_value, incoming_updated_at):
    """Newest-edit-wins for single-value fields.

    Rules, in order:
    - An empty/identical incoming value never changes anything.
    - If BOTH sides lack a timestamp and we already hold a value, keep it:
      with no evidence of recency, the order providers happen to be pulled
      in must not decide the winner (this once let a wrong photo pulled
      last silently overwrite the correct one pulled first).
    - A missing current timestamp otherwise means "unknown, take incoming".
    - Otherwise the incoming value wins only when STRICTLY newer - equal
      timestamps keep the current value, again so pull order can't flip
      a tie back and forth between providers.
    """
    if not incoming_value or incoming_value == current_value:
        return current_value, current_updated_at
    if current_value and not current_updated_at and not incoming_updated_at:
        return current_value, current_updated_at
    if not current_updated_at or incoming_updated_at > current_updated_at:
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
