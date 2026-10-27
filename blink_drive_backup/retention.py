"""Pure retention policy: preserve the newest clips within a decimal GB budget."""
def retention_plan(files, budget):
    if budget <= 0:
        raise ValueError("Budget must be positive")
    total = sum(int(f["size"]) for f in files)
    remove = []
    for item in sorted(files, key=lambda f: (f["recorded"], f["id"])):
        if total <= budget:
            break
        remove.append(item)
        total -= int(item["size"])
    return remove
