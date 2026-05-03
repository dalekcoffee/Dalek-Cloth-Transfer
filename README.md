# Dalek Cloth Transfer

A Blender addon for transferring Booth-style clothing armatures and meshes onto a base avatar armature. Handles pose matching, bone merging, mesh re-parenting, and shape-key-safe mesh baking in one workflow.

**Version:** 1.0.0  
**Blender:** 3.0+  
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
- Both armatures: name, type, location, rotation, scale, pose position, bone count, constraints
- Bone comparison: shared / only-cloth / only-base counts and full name lists
- World-space positions of all shared bones (base vs cloth, with delta distance)
- Robust alignment estimate: scale factor and translation vector (median-based, outlier-resistant)
- Per-bone residual after applying the estimated alignment
- Full pairwise distance ratio statistics (min / P25 / median / P75 / max / mean)

---

## Compatibility

| Blender version | Status |
|-----------------|--------|
| 3.0 – 4.x | Fully supported |
| 5.0+ | Supported — bone selection API change (`Bone.select` → `PoseBone.select`) handled automatically |

---

## License

[MIT](LICENSE)
