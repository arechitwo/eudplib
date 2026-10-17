# UnitGroup extra fields (private prototype)

This branch is an experimental extension for the arechitwo fork. It is not an upstream proposal and has not been built or tested.

Declare fields on a subclass:

```python
from eudplib import CUnit, UnitGroup

class ArmyGroup(UnitGroup):
    _fields_ = [
        ('level', 0),
        ('healing_debt', 0),
        ('target', CUnit, 0),
    ]

army = ArmyGroup(1700)
```

Alternatively, construct `UnitGroup(1700, fields=[('level', 0)])` from Python. A field can be a name (untyped, default 0), `(name, default)`, `(name, type)` (default 0), or `(name, type, default)`. Types must provide `cast`; defaults must be compile-time values. Names cannot shadow the existing loop-entry API.

```javascript
// Import the declared group into epScript; registration remains build-time.
army.add(epd);         // all fields use their defaults
army.add(epd, 3, 0);   // level=3, healing_debt=0, target=0

foreach (entry : army.cploop) {
    foreach (dead : entry.dying) {
        // Fields still belong to the dying unit in this block.
        const previousLevel = dead.level;
    }
    entry.healing_debt += 4;
    if (entry.healing_debt >= 256) {
        entry.healing_debt -= 256;
    }
    const target = entry.target;
    if (target != 0) {
        // Access target members only after checking for null.
    }
}
```

Python callers may also use `army.add(epd, level=3)`. Positional values follow declaration order; omitted values use defaults. Unknown or duplicate field arguments are rejected at compile time.

## Storage and removal

Each extra field owns an untyped EUDVArray with the same capacity as the group's EPD array. Typed reads cast the loaded runtime variable; writes keep integer 0 as an empty slot without constructing `CUnit(0)`. This prototype does not depend on the separate nullable-assignment PR.

The group stores active entries in a contiguous suffix. Removing an entry copies the first active EPD into the removed slot. The extension copies every field from that same source slot into the same destination slot before changing the active boundary. This path is shared by `entry.remove()` and automatic removal at the end of `entry.dying`.

Inactive slots are not cleared on removal: they are outside the active range, and `add` initializes every field when reusing a slot. Fields are available to the death callback before compaction, and only surviving-entry fields are accessed afterward.

## Cost and limitations

- Groups without fields keep the original runtime add/loop/removal path.
- Groups with fields maintain one current-slot index, not one cursor per field.
- Fields are loaded only when accessed. Repeated accesses are not cached; cache frequently read values in a local variable.
- Removal costs one read/write pair per field. There is no unit scan or field-value search.
- Storage grows by one capacity-sized VArray per field. Large per-player groups with many fields can use substantial map payload space.
- Fields belong to a registration in this group, not globally to a CUnit. Adding a unit to another group uses that group's defaults unless values are explicitly passed. There is no transfer API yet.
- The existing UnitGroup assumptions still apply: declare groups at build time, service `entry.dying`, do not exceed capacity, and avoid registering the same live unit twice.

No tests, lint, builds, or local euddraft binary installation have been performed for this prototype.
