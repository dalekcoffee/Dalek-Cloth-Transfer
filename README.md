# Dalek Cloth Transfer

A Blender addon for transferring Booth-style clothing armatures and meshes onto a base avatar armature. Handles pose matching, bone merging, mesh re-parenting, and shape-key-safe mesh baking in one workflow.

**Version:** 1.0.5  
**Blender:** 3.2+  
**Location:** `View3D > Sidebar > Cloth Transfer`  
**Category:** Rigging

---

## Installation

1. Download `dalek_cloth_transfer.py`
2. In Blender: **Edit > Preferences > Add-ons > Install**
3. Select the downloaded file and enable **Dalek Cloth Transfer**

---

## Workflow Overview

```
1. Pick Base Armature  →  your avatar
2. Pick Clothing Armature  →  the outfit
3. Configure options  →  bones to transfer, bones to delete
4. Transfer Clothing  →  one click
5. Delete Clothing Armature  →  cleanup
```

---

## Features

### Armature Picker

Pick the **Base Armature** (your avatar) and the **Clothing Armature** (the outfit) via object pickers filtered to armature types only.

---

### Compatibility Metrics

Displayed live as soon as both armatures are picked:

| Metric | Meaning |
|--------|---------|
| **Match %** | Percentage of base bones whose name exists on the clothing armature — the primary measure of rig compatibility |
| **Shared** | Absolute count of shared bone names — these drive pose alignment |
| **Only-cloth** | Bones unique to the clothing armature (candidates for transfer) |
| **Only-base** | Bones unique to the base armature (candidates for deletion) |

Each row has an extended tooltip explaining what the value means for the transfer.

---

### Options

#### Skip Body-Part Bones *(default: on)*

Automatically unchecks clothing bones that appear to be duplicates of base anatomy — for example `Head_end` when the base already has `Head`, or extra finger/toe variants, hair bones, etc.

- Uses a keyword list covering all major anatomy terms (`head`, `neck`, `shoulder`, `spine`, `arm`, `hand`, `finger`, `hip`, `leg`, `foot`, `toe`, `eye`, `tail`, `hair`, and more)
- Also strips common Blender suffixes (`_end`, `.001`) before name matching
- When applied to the **Delete** list, protects anatomy bones on the base from accidental deletion
- Toggling **off** re-enables only the bones that were auto-skipped — anything you manually toggled is left alone

#### Transfer New Bones *(default: on)*

Copies bones that exist on the clothing armature but not on the base into the base armature. Useful for outfits with physics chains, skirt bones, accessory bones, or any rig extras the base doesn't have.

- Preserves full bone hierarchy (parent/child relationships)
- Preserves bone roll and connect state
- **Per-bone list** with hierarchy indentation — check or uncheck individual bones
- **All / None** quick-select buttons
- **Highlight** button — switches to the clothing armature in Pose mode and selects exactly the bones that would be transferred, so you can inspect them before committing

##### Unmapped Parent Overrides

When a cloth-only bone's parent doesn't exist on the base armature and isn't itself being transferred, the transfer would create a parentless (root) bone unexpectedly. The addon detects these cases and surfaces them before running:

- Transfer is **blocked** until every unmapped parent is resolved
- A fuzzy-name suggestion is shown for each (e.g. if the cloth parent is `J_Bip_C_Hips`, it suggests `Hips` from the base)
- A `prop_search` field lets you pick any base bone as the new parent
- Leave it blank to intentionally add the bone as a root

#### Delete Extra Bones *(default: off)*

Removes bones on the base armature that don't exist on the clothing armature after transfer.

> **Warning:** This can break deformation on the original base mesh. Only enable if you intend the base rig to fully match the clothing rig.

- Per-bone list with the same hierarchy-indent UI as the transfer list
- **All / None** quick-select buttons
- **Highlight** button — switches to the base armature in Pose mode and selects the bones that would be deleted

---

### Match Pose

Aligns the clothing armature's **rest pose** to match the base armature's rest pose for all shared bones, then applies it as the new rest pose. Clothing meshes are automatically reshaped so the visual result stays the same.

This step also runs automatically as part of **Transfer Clothing**. Use it standalone if you want to align the rigs without doing a full transfer (e.g. to inspect the result first).

**How it works:**
1. For every shared bone, computes the target matrix in the clothing armature's local space
2. Walks the bone hierarchy in parent-first order, computing each bone's `matrix_basis` analytically without relying on the depsgraph to catch up — this avoids the child-bone offset bug that affected naive implementations
3. Bakes the current deformation into every clothing mesh using **Python linear blend skinning (LBS)** — works through all shape keys, unlike Blender's built-in `modifier_apply`
4. Applies the pose as the new rest pose via `pose.armature_apply`
5. Re-adds a fresh Armature modifier on each baked mesh

---

### Transfer Clothing

The main operator. Runs the complete transfer in one click:

1. **Pose match** — aligns the clothing rig's rest pose to the base (see above)
2. **Transfer new bones** — copies cloth-only bones into the base armature (if enabled and all unmapped parents are resolved)
3. **Mesh re-parent** — any mesh parented to or deformed by the clothing armature is re-pointed to the base armature; world-space position is preserved
4. **Delete extra bones** — removes base-only bones (if enabled)

On completion, reports: match percentage, shared bone count, cloth-only count, and base-only count.

---

### Delete Clothing Armature

One-click cleanup that removes the source clothing armature object and its data block once the transfer is complete.

- Only enabled **after** Transfer Clothing has run (the button is greyed out while any mesh still references the clothing armature as a parent or Armature modifier target)
- Fully undoable

---

### Debug Dump

Writes a detailed diagnostic report to:
- A **Text Editor block** named `ClothTransfer_Debug` (copy/share from there)
- Your **system clipboard**
- A **`ClothTransfer_Debug.txt` file** in your `.blend` file's directory (or system temp if the file is unsaved)

Report includes:
- Plugin version and Blender version
- The `.blend` file name and full path (so multiple dumps are easy to tell apart)
- Both armatures: name, type, location, rotation, scale, pose position, bone count, constraints
- Bone comparison: shared / only-cloth / only-base counts and full name lists
- World-space positions of all shared bones (base vs cloth, with delta distance)
- Robust alignment estimate: scale factor and translation vector (median-based, outlier-resistant)
- Per-bone residual after applying the estimated alignment
- Full pairwise distance ratio statistics (min / P25 / median / P75 / max / mean)

#### Full bone detail *(per armature)*

A complete, read-only, per-bone table in armature/rest space — the data an
external auto-IK / humanoid solver keys on. For **every** bone (no truncation):

- Index in `data.bones` (export order), parent (name + index)
- `use_deform`, `use_connect`, `use_inherit_rotation`, child count, descendant count
- `head_local` / `tail_local` / length, plus **world-space** head & tail
- Local **X / Y / Z axes** (Y is the head→tail direction; X/Z encode bone roll)
- Bone-collection (4.0+) or layer (3.x) membership
- A `parent -> [children in data order]` listing, flagging parents with multiple children
- A note if the object's transform has non-identity / non-uniform scale

This section works on a **single armature** too: if no Base/Clothing is set it
falls back to the active object, then to every armature in the scene — so you can
run it on a finished model (after the clothing rig was deleted) and on a
known-good model, then diff the two text files.

#### A/B bone diff *(when both armatures are set)*

Load a **known-good model as Base** and a **suspect model as Clothing** in the
same scene, then dump. Instead of forcing a manual diff across two files, the
report lists **only the bones that differ** (matched by name), with both values,
for: length, head/tail direction, roll, head/tail position, `use_deform`,
`use_connect`, `use_inherit_rotation`, parent, child order, and bone-collection
membership. It finishes with a **limb-tip "longest child" comparison** that flags
any limb where the longest child of a tip parent (e.g. `Lower Arm`) changed
between the two models — the exact failure mode that makes an auto-IK solver pick
a twist/support bone as the hand. This is a fast "what did the transfer change?"
report.

#### Mesh skin / vertex-weight diagnostics

Resonite builds its humanoid/IK rig from the **skinned mesh's bone bindings**, not
the armature alone — so a bone that looks correct in the armature can still be
mis-detected if the mesh carries no weight on it. For every mesh in the scene this
reports: vertex count, vertex-group count, armature-modifier target, and the list
of **empty vertex groups** (bones the mesh declares but doesn't actually skin to).
It finishes with a **limb-tip weight focus**: for each limb-tip parent, the
verts/weight carried by each child, flagging any `Hand`/`Foot` bone with
**`[NO WEIGHT]`**. A limb-tip with no weight while its support sibling has weight
is the prime reason an auto-IK solver binds to the support bone.

#### Limb end-effector focus

For each bone whose name contains `hand` / `foot` / `toe` / `head`, lists its
parent and **all** of that parent's children ranked by length, with descendant
count, deform flag, direction, and world-space tail. This is exactly the choice
an auto-IK solver faces when picking a limb tip — if a *support* bone is longer
or extends further than the real `Hand`/`Foot` bone, a length-based solver will
mis-pick it, and that shows up here immediately. The dump explicitly flags when
the longest child of a limb-tip parent is **not** a limb-tip-named bone.

---

## Compatibility

| Blender version | Status |
|-----------------|--------|
| 3.0 – 3.1 | Not supported — requires `Context.temp_override` (added in 3.2) |
| 3.2 – 4.x | Fully supported |
| 5.0+ | Supported — bone selection API change (`Bone.select` → `PoseBone.select`) handled automatically |

---

## License

[MIT](LICENSE)
