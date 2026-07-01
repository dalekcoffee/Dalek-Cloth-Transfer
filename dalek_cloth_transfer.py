bl_info = {
    "name": "Dalek Cloth Transfer",
    "author": "Dalek",
    "version": (1, 0, 5),
    # Requires 3.2+: the addon depends on Context.temp_override, which was
    # added in Blender 3.2. On 3.0/3.1 every operator here raises AttributeError.
    "blender": (3, 2, 0),
    "location": "View3D > Sidebar > Cloth Transfer",
    "description": "Transfer Booth-style clothing armatures and meshes onto a base avatar armature",
    "category": "Rigging",
}

__version__ = bl_info["version"]
__version_str__ = ".".join(str(x) for x in __version__)

import difflib
import math
import os
import re
import tempfile

import bpy
from bpy.props import (
    PointerProperty,
    BoolProperty,
    FloatProperty,
    StringProperty,
    IntProperty,
    CollectionProperty,
)
from bpy.types import Operator, Panel, PropertyGroup, UIList
from mathutils import Vector, Matrix


def _is_armature(self, obj):
    return obj.type == 'ARMATURE'


# Blender 5.0 moved bone selection state from Bone.select to PoseBone.select.
_BLENDER_5 = bpy.app.version >= (5, 0, 0)

# Guard so cascade-driven writes to BoneItem.enabled don't re-trigger the cascade.
_CASCADE_GUARD = {"depth": 0}

# Anatomy words used by the "skip body-part bones" heuristic. Deliberately
# excludes generic terms ("back", "front", "root", "side") so clothing bones
# named CoatBack/CoatFront/Coat_Root stay enabled.
_BODY_PART_KEYWORDS = (
    "head", "neck", "shoulder", "chest", "spine", "torso", "breast",
    "arm", "forearm", "elbow", "wrist", "hand",
    "finger", "thumb", "index", "middle", "ring", "little", "pinky",
    "hip", "leg", "thigh", "knee", "ankle", "foot", "toe",
    "eye", "ear", "cheek", "jaw", "lip", "mouth", "nose", "tongue", "tooth",
    "tail", "hair",
)

# Trailing _end / .001 / chains thereof (e.g. "Head_end", "Foo.001_end_end").
_BONE_SUFFIX_RE = re.compile(r"(?:_end|\.\d+)+$", re.IGNORECASE)


def _strip_bone_suffix(name):
    return _BONE_SUFFIX_RE.sub("", name)


def _bone_keywords_in(name):
    nlow = name.lower()
    return {kw for kw in _BODY_PART_KEYWORDS if kw in nlow}


def _make_transfer_skip_predicate(base_armature):
    """Return a predicate(name) → bool that's True when a cloth-only bone is
    a likely duplicate of base anatomy (suffix-stripped match in base, or an
    anatomy keyword that the base armature also uses)."""
    if base_armature is None or base_armature.type != 'ARMATURE':
        return None
    base_names = {b.name for b in base_armature.data.bones}
    base_kws = {kw for n in base_names for kw in _bone_keywords_in(n)}

    def predicate(name):
        stripped = _strip_bone_suffix(name)
        if stripped and stripped != name and stripped in base_names:
            return True
        bone_kws = _bone_keywords_in(name)
        if bone_kws and (bone_kws & base_kws):
            return True
        return False

    return predicate


def _make_delete_skip_predicate():
    """Predicate(name) → True when a base-only bone is anatomy that we should
    not auto-delete (mesh weights on the base may depend on it)."""
    def predicate(name):
        return bool(_bone_keywords_in(name))
    return predicate


def _apply_body_part_skip(coll, armature, predicate):
    """Bulk-uncheck items matching the predicate AND their descendants in the
    same collection. Suppresses the per-item cascade callback so one user click
    doesn't trigger N callbacks. Every disabled item is tagged with
    `auto_skipped=True` so the inverse op can find it again."""
    if predicate is None:
        return
    _CASCADE_GUARD["depth"] += 1
    try:
        seeds = []
        for it in coll:
            if predicate(it.name):
                seeds.append(it.name)
                if it.enabled:
                    it.auto_skipped = True
                    it.enabled = False
        if seeds and armature is not None and armature.type == 'ARMATURE':
            data_bones = armature.data.bones
            for name in seeds:
                bone = data_bones.get(name)
                if bone is None:
                    continue
                for child in bone.children_recursive:
                    ci = coll.get(child.name)
                    if ci is None or not ci.enabled:
                        continue
                    ci.auto_skipped = True
                    ci.enabled = False
    finally:
        _CASCADE_GUARD["depth"] -= 1
    _sync_pose_selection(armature, coll)


def _undo_body_part_skip(coll, armature):
    """Re-enable items previously tagged by `_apply_body_part_skip`. Items the
    user has since touched manually have their `auto_skipped` flag cleared and
    are left alone here."""
    _CASCADE_GUARD["depth"] += 1
    try:
        for it in coll:
            if it.auto_skipped:
                it.auto_skipped = False
                it.enabled = True
    finally:
        _CASCADE_GUARD["depth"] -= 1
    _sync_pose_selection(armature, coll)


def _on_bone_item_enabled_changed(self, context):
    if _CASCADE_GUARD["depth"] > 0:
        return
    # User manually edited this checkbox → relinquish the auto-skip claim.
    if self.auto_skipped:
        self.auto_skipped = False
    # `id_data` is the Scene this PropertyGroup item lives on — robust against
    # the user editing a different scene than `context.scene` happens to be.
    p = self.id_data.cloth_transfer_props
    bone_name = self.name
    self_id = self.as_pointer()
    coll = None
    armature = None
    # Item.name is the collection key, so coll.get(name) is O(1). Pointer check
    # disambiguates the (already disjoint) case of the same name in both lists.
    for cand_coll, cand_arm in (
        (p.transfer_bone_list, p.cloth_armature),
        (p.delete_bone_list, p.base_armature),
    ):
        candidate = cand_coll.get(bone_name)
        if candidate is not None and candidate.as_pointer() == self_id:
            coll = cand_coll
            armature = cand_arm
            break
    if coll is None or armature is None or armature.type != 'ARMATURE':
        return
    bone = armature.data.bones.get(bone_name)
    if bone is None:
        return
    _CASCADE_GUARD["depth"] += 1
    try:
        if not self.enabled:
            for child in bone.children_recursive:
                ci = coll.get(child.name)
                if ci is not None and ci.enabled:
                    ci.enabled = False
        else:
            parent = bone.parent
            while parent is not None:
                pi = coll.get(parent.name)
                if pi is not None and not pi.enabled:
                    pi.enabled = True
                parent = parent.parent
    finally:
        _CASCADE_GUARD["depth"] -= 1
    _sync_pose_selection(armature, coll)


class ClothTransferBoneItem(PropertyGroup):
    name: StringProperty()
    enabled: BoolProperty(
        name="",
        description="Include this bone in the operation",
        default=True,
        update=_on_bone_item_enabled_changed,
    )
    depth: IntProperty(default=0)
    # Set True when the body-part heuristic auto-unchecked this item, so the
    # toggle can selectively re-check exactly those entries when disabled.
    # Cleared whenever the user manually edits this checkbox.
    auto_skipped: BoolProperty(default=False)


class ClothTransferParentOverride(PropertyGroup):
    bone_name: StringProperty()
    original_parent: StringProperty()
    chosen_parent: StringProperty(
        name="",
        description=(
            "Base armature bone to use as parent for this transferred bone. "
            "Leave empty to add it as a root bone"
        ),
    )


def _on_armatures_changed(self, context):
    if self.transfer_new_bones:
        _refresh_transfer_bone_list(self)
    if self.delete_extra_bones:
        _refresh_delete_bone_list(self)
    self.parent_overrides.clear()


def _on_transfer_new_bones_toggled(self, context):
    if self.transfer_new_bones:
        _refresh_transfer_bone_list(self)


def _on_delete_extra_bones_toggled(self, context):
    if self.delete_extra_bones:
        _refresh_delete_bone_list(self)


def _on_skip_body_part_bones_toggled(self, context):
    if self.skip_body_part_bones:
        if self.transfer_new_bones:
            _apply_body_part_skip(
                self.transfer_bone_list,
                self.cloth_armature,
                _make_transfer_skip_predicate(self.base_armature),
            )
        if self.delete_extra_bones:
            _apply_body_part_skip(
                self.delete_bone_list,
                self.base_armature,
                _make_delete_skip_predicate(),
            )
    else:
        if self.transfer_new_bones:
            _undo_body_part_skip(self.transfer_bone_list, self.cloth_armature)
        if self.delete_extra_bones:
            _undo_body_part_skip(self.delete_bone_list, self.base_armature)


class ClothTransferProps(PropertyGroup):
    base_armature: PointerProperty(
        name="Base Armature",
        description="Target armature — your base avatar",
        type=bpy.types.Object,
        poll=_is_armature,
        update=_on_armatures_changed,
    )
    cloth_armature: PointerProperty(
        name="Clothing Armature",
        description="Source armature — the clothing/outfit being transferred",
        type=bpy.types.Object,
        poll=_is_armature,
        update=_on_armatures_changed,
    )
    transfer_new_bones: BoolProperty(
        name="Transfer new bones",
        description=(
            "Add bones that exist on the clothing armature but not on the base "
            "to the base armature. Useful for outfits that introduce extra bones "
            "(skirt bones, accessory bones, physics chains). "
            "Disable to only use bones the base already has"
        ),
        default=True,
        update=_on_transfer_new_bones_toggled,
    )
    delete_extra_bones: BoolProperty(
        name="Delete extra bones from base",
        description=(
            "Remove bones on the base armature that don't exist on the clothing "
            "armature. WARNING: this can break deformation on the original base "
            "mesh — only enable if you intend the base rig to fully match the "
            "clothing rig"
        ),
        default=False,
        update=_on_delete_extra_bones_toggled,
    )
    skip_body_part_bones: BoolProperty(
        name="Skip body-part bones",
        description=(
            "Auto-uncheck cloth-only bones that look like duplicates of base "
            "anatomy (e.g. Head_end when base has Head, extra finger/toe "
            "variants when base already covers those, hair). For 'Delete extra', "
            "also auto-unchecks base bones whose names contain anatomy keywords "
            "so anatomy is not accidentally deleted. Toggling off re-checks the "
            "bones it auto-unchecked — anything you toggled by hand is left alone"
        ),
        default=True,
        update=_on_skip_body_part_bones_toggled,
    )
    transfer_bone_list: CollectionProperty(type=ClothTransferBoneItem)
    transfer_bone_active_index: IntProperty(default=0)
    transfer_list_expanded: BoolProperty(default=True)
    delete_bone_list: CollectionProperty(type=ClothTransferBoneItem)
    delete_bone_active_index: IntProperty(default=0)
    delete_list_expanded: BoolProperty(default=True)
    parent_overrides: CollectionProperty(type=ClothTransferParentOverride)
    parent_overrides_expanded: BoolProperty(default=True)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _shared_bone_names(base, cloth):
    base_names = {b.name for b in base.data.bones}
    cloth_names = {b.name for b in cloth.data.bones}
    return base_names & cloth_names


def _populate_bone_list(coll, armature, only_names, skip_predicate=None):
    prior = {it.name: (it.enabled, it.auto_skipped) for it in coll}
    _CASCADE_GUARD["depth"] += 1
    try:
        coll.clear()
        if armature is None or armature.type != 'ARMATURE':
            return
        only_set = set(only_names)
        if not only_set:
            return
        bones_by_name = {b.name: b for b in armature.data.bones}

        ordered = []
        visited = set()

        # Iterative parent-first walk: for each bone, walk up the chain (within
        # only_set) until hitting an already-visited node or leaving the set,
        # then commit in reverse order so parents land before children.
        for b in armature.data.bones:
            start = b.name
            if start not in only_set or start in visited:
                continue
            chain = []
            cur = start
            while cur is not None and cur in only_set and cur not in visited:
                bone_obj = bones_by_name.get(cur)
                if bone_obj is None:
                    break
                chain.append(cur)
                if bone_obj.parent is not None and bone_obj.parent.name in only_set:
                    cur = bone_obj.parent.name
                else:
                    cur = None
            for name in reversed(chain):
                visited.add(name)
                ordered.append(name)

        depths = {}
        for n in ordered:
            it = coll.add()
            it.name = n
            bone = bones_by_name.get(n)
            if bone is None or bone.parent is None or bone.parent.name not in only_set:
                d = 0
            else:
                d = depths[bone.parent.name] + 1
            depths[n] = d
            it.depth = d
            if n in prior:
                it.enabled, it.auto_skipped = prior[n]
            else:
                it.enabled = True
                it.auto_skipped = False
    finally:
        _CASCADE_GUARD["depth"] -= 1
    # Defer the actual auto-skip to _apply_body_part_skip so cascade-to-
    # descendants is handled in one place.
    if skip_predicate is not None:
        _apply_body_part_skip(coll, armature, skip_predicate)


def _refresh_bone_list(p, kind):
    """Rebuild the transfer or delete bone list. `kind` is "transfer" (cloth-
    only bones, populated on cloth armature) or "delete" (base-only bones,
    populated on base armature)."""
    if kind == "transfer":
        coll = p.transfer_bone_list
        source_arm, ref_arm = p.cloth_armature, p.base_armature
    else:
        coll = p.delete_bone_list
        source_arm, ref_arm = p.base_armature, p.cloth_armature

    if (
        source_arm is None
        or ref_arm is None
        or source_arm == ref_arm
        or source_arm.type != 'ARMATURE'
        or ref_arm.type != 'ARMATURE'
    ):
        _CASCADE_GUARD["depth"] += 1
        try:
            coll.clear()
        finally:
            _CASCADE_GUARD["depth"] -= 1
        return

    shared = _shared_bone_names(p.base_armature, p.cloth_armature)
    only_names = [b.name for b in source_arm.data.bones if b.name not in shared]
    if p.skip_body_part_bones:
        pred = (
            _make_transfer_skip_predicate(p.base_armature)
            if kind == "transfer"
            else _make_delete_skip_predicate()
        )
    else:
        pred = None
    _populate_bone_list(coll, source_arm, only_names, skip_predicate=pred)


def _refresh_transfer_bone_list(p):
    _refresh_bone_list(p, "transfer")


def _refresh_delete_bone_list(p):
    _refresh_bone_list(p, "delete")


def _enabled_names(coll):
    return {it.name for it in coll if it.enabled}


def _get_orphan_parents(p):
    """Return [(bone_name, original_parent_name)] for enabled cloth-only bones
    whose cloth-side parent name resolves neither to a base bone nor to another
    enabled cloth-only bone."""
    if (
        p.cloth_armature is None
        or p.base_armature is None
        or p.cloth_armature == p.base_armature
        or p.cloth_armature.type != 'ARMATURE'
        or p.base_armature.type != 'ARMATURE'
        or not p.transfer_new_bones
    ):
        return []
    enabled_cloth_only = _enabled_names(p.transfer_bone_list)
    if not enabled_cloth_only:
        return []
    base_names = {b.name for b in p.base_armature.data.bones}
    orphans = []
    for cb in p.cloth_armature.data.bones:
        if cb.name not in enabled_cloth_only or cb.parent is None:
            continue
        pname = cb.parent.name
        if pname in base_names or pname in enabled_cloth_only:
            continue
        orphans.append((cb.name, pname))
    return orphans


def _suggest_parent(parent_name, base_names):
    if not parent_name or not base_names:
        return ""
    matches = difflib.get_close_matches(parent_name, list(base_names), n=1, cutoff=0.5)
    return matches[0] if matches else ""


def _sync_parent_overrides(p):
    """Reconcile p.parent_overrides with the current orphan set.

    Returns (added, total). `added` counts entries newly inserted by this
    call — when > 0 the user has new orphans to review. Existing entries
    keep their `chosen_parent` so the user's prior picks are not lost."""
    orphans = _get_orphan_parents(p)
    orphan_names = {b for b, _ in orphans}
    coll = p.parent_overrides

    for i in range(len(coll) - 1, -1, -1):
        if coll[i].bone_name not in orphan_names:
            coll.remove(i)

    existing = {it.bone_name for it in coll}
    base_names = (
        {b.name for b in p.base_armature.data.bones}
        if p.base_armature is not None and p.base_armature.type == 'ARMATURE'
        else set()
    )

    added = 0
    for bone_name, parent_name in orphans:
        if bone_name in existing:
            continue
        item = coll.add()
        item.bone_name = bone_name
        item.original_parent = parent_name
        item.chosen_parent = _suggest_parent(parent_name, base_names)
        added += 1
    return added, len(orphans)


def _ensure_object_mode(context):
    if context.object is not None and context.object.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')


def _activate(context, obj):
    for o in context.view_layer.objects:
        o.select_set(False)
    obj.select_set(True)
    context.view_layer.objects.active = obj


def _set_mode_on(context, obj, mode):
    """Switch `obj` into `mode`, forcing the operator's poll to see `obj` as
    the active object. Plain `bpy.ops.object.mode_set` reads context.object,
    which is not always refreshed in the same Python tick after assigning
    `view_layer.objects.active = obj` — leading to "Context missing active
    object" errors when the click came from a sidebar panel."""
    with context.temp_override(active_object=obj, object=obj):
        bpy.ops.object.mode_set(mode=mode)


def _pairwise_distance_ratios(base_pts, cloth_pts):
    """Pairwise base/cloth length ratios for every (i,j) pair where both legs
    are non-degenerate. Used by both alignment estimation and debug stats."""
    n = min(len(base_pts), len(cloth_pts))
    ratios = []
    for i in range(n):
        for j in range(i + 1, n):
            d_b = (base_pts[i] - base_pts[j]).length
            d_c = (cloth_pts[i] - cloth_pts[j]).length
            if d_c > 1e-6 and d_b > 1e-6:
                ratios.append(d_b / d_c)
    return ratios


def _robust_alignment(base_pts, cloth_pts):
    """Estimate uniform scale + translation that aligns cloth_pts to base_pts.

    Uses median of all pairwise distance ratios for the scale (immune to a
    handful of outlier shared bones — e.g. mismatched hair or hat bones that
    extend far beyond the body), and median-per-axis for translation.
    """
    n = len(base_pts)
    if n < 2:
        return 1.0, Vector((0.0, 0.0, 0.0))

    ratios = _pairwise_distance_ratios(base_pts, cloth_pts)
    if not ratios:
        return 1.0, Vector((0.0, 0.0, 0.0))

    ratios.sort()
    s = ratios[len(ratios) // 2]

    diffs_x = sorted(base_pts[i].x - s * cloth_pts[i].x for i in range(n))
    diffs_y = sorted(base_pts[i].y - s * cloth_pts[i].y for i in range(n))
    diffs_z = sorted(base_pts[i].z - s * cloth_pts[i].z for i in range(n))
    t = Vector((diffs_x[n // 2], diffs_y[n // 2], diffs_z[n // 2]))
    return s, t


def _bake_pose_to_mesh_lbs(mesh, cloth):
    """Bake the cloth armature's current pose deformation into a mesh's vertex
    data using linear blend skinning, then remove the armature modifier.

    Works through shape keys (the same per-vertex deformation matrix is applied
    to each shape key's absolute vertex positions, so deltas are preserved).
    All deformed positions are computed up front, then applied atomically — if
    anything raises mid-computation, the mesh is left untouched.
    """
    arm_mod = next(
        (m for m in mesh.modifiers if m.type == 'ARMATURE' and m.object == cloth),
        None,
    )
    if arm_mod is None:
        return False

    bone_deform_arm = {}
    for pb in cloth.pose.bones:
        rest_local = pb.bone.matrix_local
        bone_deform_arm[pb.name] = pb.matrix @ rest_local.inverted()

    arm_to_mesh = mesh.matrix_world.inverted() @ cloth.matrix_world
    mesh_to_arm = arm_to_mesh.inverted()
    bone_deform = {n: arm_to_mesh @ M @ mesh_to_arm for n, M in bone_deform_arm.items()}

    vg_name_by_idx = {vg.index: vg.name for vg in mesh.vertex_groups}
    vert_count = len(mesh.data.vertices)
    identity = Matrix.Identity(4)

    deform_mats = []
    for v in mesh.data.vertices:
        weights = []
        for ge in v.groups:
            vg_name = vg_name_by_idx.get(ge.group)
            if vg_name and vg_name in bone_deform and ge.weight > 0.0:
                weights.append((vg_name, ge.weight))
        if not weights:
            deform_mats.append(identity)
            continue
        total = sum(w for _, w in weights)
        if total <= 0.0:
            deform_mats.append(identity)
            continue
        rows = [[0.0] * 4 for _ in range(4)]
        for bone_name, w in weights:
            M = bone_deform[bone_name]
            nw = w / total
            for r in range(4):
                for c in range(4):
                    rows[r][c] += nw * M[r][c]
        deform_mats.append(Matrix(rows))

    new_basis = [deform_mats[i] @ mesh.data.vertices[i].co for i in range(vert_count)]
    new_sk_data = {}
    if mesh.data.shape_keys:
        for sk in mesh.data.shape_keys.key_blocks:
            new_sk_data[sk.name] = [deform_mats[i] @ sk.data[i].co for i in range(vert_count)]

    if mesh.data.shape_keys:
        for sk in mesh.data.shape_keys.key_blocks:
            new_list = new_sk_data[sk.name]
            for i in range(vert_count):
                sk.data[i].co = new_list[i]
    for i in range(vert_count):
        mesh.data.vertices[i].co = new_basis[i]

    mesh.modifiers.remove(arm_mod)
    return True


# Armature-modifier settings worth carrying across the remove/re-add that the
# LBS bake performs. Stack *order* matters too (a Subdivision/Solidify after the
# armature must stay after it), so the snapshot also records the index.
_ARM_MOD_PROPS = (
    "show_viewport", "show_render", "show_in_editmode", "show_on_cage",
    "use_vertex_groups", "use_bone_envelopes", "use_deform_preserve_volume",
    "use_multi_modifier", "vertex_group", "invert_vertex_group",
)


def _snapshot_arm_modifier(mesh, arm_obj):
    """Capture name, stack index and settings of the ARMATURE modifier on `mesh`
    that targets `arm_obj`, so an equivalent one can be restored in the same
    place after the LBS bake removes it. Returns None if not found."""
    for i, m in enumerate(mesh.modifiers):
        if m.type == 'ARMATURE' and m.object == arm_obj:
            snap = {"name": m.name, "index": i}
            for prop in _ARM_MOD_PROPS:
                if hasattr(m, prop):
                    snap[prop] = getattr(m, prop)
            return snap
    return None


def _restore_arm_modifier(context, mesh, arm_obj, snap):
    """Re-create the armature modifier captured by `_snapshot_arm_modifier`,
    restoring its settings and original stack position. Falls back gracefully
    (modifier left at the end of the stack) if anything goes wrong."""
    name = snap.get("name", "Armature") if snap else "Armature"
    mod = mesh.modifiers.new(name=name, type='ARMATURE')
    mod.object = arm_obj
    if not snap:
        return mod
    for prop in _ARM_MOD_PROPS:
        if prop in snap and hasattr(mod, prop):
            try:
                setattr(mod, prop, snap[prop])
            except Exception:
                pass
    # new() appends to the end; move it back to where it was if other modifiers
    # were sitting after it (e.g. Subdivision), or their evaluation order flips.
    target = snap.get("index")
    if target is not None and target < len(mesh.modifiers) - 1:
        try:
            with context.temp_override(active_object=mesh, object=mesh):
                bpy.ops.object.modifier_move_to_index(modifier=mod.name, index=target)
        except Exception:
            pass
    return mod


def _match_pose(context, base, cloth, shared):
    """Match cloth's rest pose to base's rest pose for shared bones, mesh data included.

    1. Compute each shared bone's target matrix in cloth's armature space.
    2. Walk the cloth bone hierarchy and compute matrix_basis directly from
       the predicted parent state (no reliance on Blender's depsgraph having
       caught up between bone assignments — that's what was leaving children
       like the foot a few cm off in v1.0.0).
    3. Bake current deformation into each cloth-deformed mesh via Python LBS
       (works through shape keys, unlike modifier_apply). Removes the
       armature modifier; we'll re-add it after.
    4. pose.armature_apply on the armature — purely a bone update now, since
       the meshes have no armature modifier targeting cloth at this point.
    5. Re-add a fresh armature modifier on each baked mesh.
    """
    if not shared:
        return

    _ensure_object_mode(context)

    cloth_inv = cloth.matrix_world.inverted()
    targets = {}
    for name in shared:
        targets[name] = cloth_inv @ (base.matrix_world @ base.data.bones[name].matrix_local)

    _activate(context, cloth)

    _set_mode_on(context, cloth, 'EDIT')
    disconnected = []
    for ebone in cloth.data.edit_bones:
        if ebone.use_connect:
            ebone.use_connect = False
            disconnected.append(ebone.name)

    _set_mode_on(context, cloth, 'POSE')

    parent_of = {}
    for b in cloth.data.bones:
        parent_of[b.name] = b.parent.name if b.parent is not None else None

    ordered_names = []
    visited = set()

    # Iterative parent-first ordering — avoids Python recursion limits on deep
    # bone chains (long hair/skirt physics chains can hit it).
    for start in list(parent_of.keys()):
        if start in visited:
            continue
        chain = []
        cur = start
        while cur is not None and cur not in visited:
            chain.append(cur)
            cur = parent_of.get(cur)
        for name in reversed(chain):
            visited.add(name)
            ordered_names.append(name)

    bone_eval = {}

    for name in ordered_names:
        bone = cloth.data.bones.get(name)
        if bone is None:
            continue
        rest_local = bone.matrix_local
        parent_name = parent_of.get(name)
        if parent_name is not None:
            parent_eval = bone_eval[parent_name]
            parent_bone = cloth.data.bones.get(parent_name)
            parent_rest = parent_bone.matrix_local if parent_bone is not None else Matrix.Identity(4)
            full_chain = parent_eval @ (parent_rest.inverted() @ rest_local)
        else:
            full_chain = rest_local.copy()

        if name in shared:
            target = targets[name]
            cloth.pose.bones[name].matrix_basis = full_chain.inverted() @ target
            bone_eval[name] = target
        else:
            bone_eval[name] = full_chain

    context.view_layer.update()
    _set_mode_on(context, cloth, 'OBJECT')

    cloth_meshes = []
    for o in bpy.data.objects:
        if o.type != 'MESH':
            continue
        mod = next(
            (m for m in o.modifiers if m.type == 'ARMATURE' and m.object == cloth),
            None,
        )
        if mod is None:
            continue
        cloth_meshes.append(o)

    # Snapshot each mesh's armature modifier (settings + stack index) BEFORE the
    # bake removes it, so restoration preserves order and configuration.
    mod_snapshots = {o: _snapshot_arm_modifier(o, cloth) for o in cloth_meshes}

    baked = []
    failed = []
    for mesh in cloth_meshes:
        try:
            if _bake_pose_to_mesh_lbs(mesh, cloth):
                baked.append(mesh)
            else:
                failed.append(mesh.name)
        except Exception as e:
            print(f"[ClothTransfer] LBS bake failed on {mesh.name}: {e}")
            failed.append(mesh.name)

    _activate(context, cloth)
    _set_mode_on(context, cloth, 'POSE')
    with context.temp_override(active_object=cloth, object=cloth):
        bpy.ops.pose.armature_apply(selected=False)
    _set_mode_on(context, cloth, 'OBJECT')

    for mesh in baked:
        _restore_arm_modifier(context, mesh, cloth, mod_snapshots.get(mesh))

    print(
        f"[ClothTransfer v{__version_str__}] Match Pose: "
        f"{len(shared)} bones aligned, {len(disconnected)} bones disconnected, "
        f"{len(cloth_meshes)} cloth meshes "
        f"({len(baked)} baked via LBS, {len(failed)} failed: {failed})"
    )


def _transfer_new_bones(context, base, cloth, shared, allowed_names=None, parent_overrides=None):
    """Copy bones present in cloth but not in base into base armature.

    parent_overrides: optional dict {cloth_bone_name: base_parent_name_or_empty}.
    An empty string means "make this bone a root in base"; a non-empty value
    must name an existing base bone to use as parent. Bones not in the map fall
    back to the default name-match behavior."""
    cloth_only = [b for b in cloth.data.bones if b.name not in shared]
    if allowed_names is not None:
        cloth_only = [b for b in cloth_only if b.name in allowed_names]
    if not cloth_only:
        return

    cloth_only_names = {b.name for b in cloth_only}

    ordered = []
    visited = set()

    # Iterative parent-first ordering within cloth_only_names — same logic as
    # the recursive form, restated to dodge the Python recursion limit.
    for start in cloth_only:
        if start.name in visited:
            continue
        chain = []
        cur = start
        while cur is not None and cur.name not in visited:
            chain.append(cur)
            if cur.parent is not None and cur.parent.name in cloth_only_names:
                cur = cur.parent
            else:
                cur = None
        for bone in reversed(chain):
            visited.add(bone.name)
            ordered.append(bone)

    _ensure_object_mode(context)
    _activate(context, base)
    _set_mode_on(context, base, 'EDIT')

    base_inv = base.matrix_world.inverted()
    base_inv_3 = base_inv.to_3x3()
    cloth_3 = cloth.matrix_world.to_3x3()
    eb = base.data.edit_bones

    for cb in ordered:
        if cb.name in eb:
            continue
        head_world = cloth.matrix_world @ cb.head_local
        tail_world = cloth.matrix_world @ cb.tail_local
        z_world = cloth_3 @ (cb.matrix_local.to_3x3() @ Vector((0.0, 0.0, 1.0)))

        nb = eb.new(cb.name)
        nb.head = base_inv @ head_world
        nb.tail = base_inv @ tail_world
        nb.align_roll(base_inv_3 @ z_world)

        override = parent_overrides.get(cb.name) if parent_overrides else None
        if override is not None:
            if override and override in eb:
                nb.parent = eb[override]
                nb.use_connect = False
        elif cb.parent is not None and cb.parent.name in eb:
            nb.parent = eb[cb.parent.name]
            try:
                nb.use_connect = cb.use_connect
            except Exception:
                pass

    _set_mode_on(context, base, 'OBJECT')


def _reparent_meshes(context, base, cloth):
    """Any mesh parented to cloth or with an armature modifier targeting cloth → base."""
    cloth_meshes = []
    for o in bpy.data.objects:
        if o.type != 'MESH':
            continue
        parented = (o.parent == cloth)
        modded = any(m.type == 'ARMATURE' and m.object == cloth for m in o.modifiers)
        if parented or modded:
            cloth_meshes.append(o)

    for mesh in cloth_meshes:
        world = mesh.matrix_world.copy()
        if mesh.parent == cloth:
            mesh.parent = base
            mesh.matrix_parent_inverse.identity()
            mesh.matrix_world = world
        for m in mesh.modifiers:
            if m.type == 'ARMATURE' and m.object == cloth:
                m.object = base


def _delete_extra_bones(context, base, original_cloth_bone_names, allowed_names=None):
    """Remove bones in base that were not present on the cloth armature originally."""
    extras = [b.name for b in base.data.bones if b.name not in original_cloth_bone_names]
    if allowed_names is not None:
        extras = [n for n in extras if n in allowed_names]
    if not extras:
        return

    _ensure_object_mode(context)
    _activate(context, base)
    _set_mode_on(context, base, 'EDIT')
    eb = base.data.edit_bones
    for name in extras:
        if name in eb:
            eb.remove(eb[name])
    _set_mode_on(context, base, 'OBJECT')


# ---------------------------------------------------------------------------
# operators
# ---------------------------------------------------------------------------

def _validate_armatures(self, p):
    base, cloth = p.base_armature, p.cloth_armature
    if not base or not cloth:
        self.report({'ERROR'}, "Pick both armatures first")
        return None
    if base == cloth:
        self.report({'ERROR'}, "Base and clothing armatures must be different")
        return None
    return base, cloth


class CLOTH_TRANSFER_OT_match_pose(Operator):
    bl_idname = "cloth_transfer.match_pose"
    bl_label = "Match Pose"
    bl_description = (
        "Align the clothing armature's rest pose to the base's rest pose for "
        "shared bones, then apply as the new rest pose. Clothing mesh data is "
        "automatically reshaped so the visual stays the same"
    )
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        p = context.scene.cloth_transfer_props
        return p.base_armature and p.cloth_armature and p.base_armature != p.cloth_armature

    def execute(self, context):
        p = context.scene.cloth_transfer_props
        result = _validate_armatures(self, p)
        if result is None:
            return {'CANCELLED'}
        base, cloth = result

        shared = _shared_bone_names(base, cloth)
        if not shared:
            self.report({'ERROR'}, "No shared bones — nothing to align")
            return {'CANCELLED'}

        _ensure_object_mode(context)
        _match_pose(context, base, cloth, shared)
        self.report({'INFO'}, f"Pose matched on {len(shared)} shared bones")
        return {'FINISHED'}


class CLOTH_TRANSFER_OT_transfer(Operator):
    bl_idname = "cloth_transfer.transfer"
    bl_label = "Transfer Clothing"
    bl_description = (
        "Match the clothing armature to the base (scale + pose), then re-parent "
        "clothing meshes onto the base armature"
    )
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        p = context.scene.cloth_transfer_props
        return (
            p.base_armature is not None
            and p.cloth_armature is not None
            and p.base_armature != p.cloth_armature
        )

    def execute(self, context):
        p = context.scene.cloth_transfer_props
        base, cloth = p.base_armature, p.cloth_armature

        base_names = {b.name for b in base.data.bones}
        cloth_names = {b.name for b in cloth.data.bones}
        shared = base_names & cloth_names
        only_cloth_count = len(cloth_names - base_names)
        only_base_count = len(base_names - cloth_names)
        ratio = len(shared) / max(len(base_names), 1)

        overrides_map = None
        if p.transfer_new_bones:
            _refresh_transfer_bone_list(p)
            added, total = _sync_parent_overrides(p)
            if added > 0:
                self.report(
                    {'WARNING'},
                    f"{added} unmapped parent(s) detected — review the Unmapped Parents "
                    f"section, then click Transfer Clothing again",
                )
                return {'CANCELLED'}
            overrides_map = {it.bone_name: it.chosen_parent for it in p.parent_overrides}

        original_cloth_bones = {b.name for b in cloth.data.bones}

        _ensure_object_mode(context)
        _match_pose(context, base, cloth, shared)

        if p.transfer_new_bones:
            allowed_transfer = _enabled_names(p.transfer_bone_list)
            _transfer_new_bones(
                context, base, cloth, shared,
                allowed_names=allowed_transfer,
                parent_overrides=overrides_map,
            )

        _reparent_meshes(context, base, cloth)

        if p.delete_extra_bones:
            _refresh_delete_bone_list(p)
            allowed_delete = _enabled_names(p.delete_bone_list)
            _delete_extra_bones(context, base, original_cloth_bones, allowed_names=allowed_delete)

        p.parent_overrides.clear()
        _activate(context, base)
        self.report(
            {'INFO'},
            f"Transfer complete — match {ratio * 100:.0f}%  |  "
            f"shared {len(shared)}  |  only-cloth {only_cloth_count}  |  "
            f"only-base {only_base_count}",
        )
        return {'FINISHED'}


# Names that an auto-IK / humanoid solver tries to resolve to a single limb-tip
# bone. When a limb-tip parent has several children, the solver disambiguates by
# geometry — so these are the chains we surface in full in the debug dump.
_LIMB_END_KEYWORDS = ("hand", "foot", "toe", "head")


def _fmt_v(v, p=4):
    return f"({v.x:+.{p}f}, {v.y:+.{p}f}, {v.z:+.{p}f})"


def _bone_local_axes(bone):
    """Bone's local X/Y/Z axes expressed in armature (rest) space. The Y axis is
    the head→tail direction; X/Z together encode the bone roll. Comparing these
    between two models reveals orientation/roll differences that a name+position
    diff would miss."""
    m = bone.matrix_local.to_3x3()
    return m.col[0], m.col[1], m.col[2]


def _bone_collections_str(bone):
    """Membership in bone collections (Blender 4.0+) or armature layers (3.x),
    whichever this build exposes."""
    cols = getattr(bone, "collections", None)
    if cols is not None:
        names = [c.name for c in cols]
        return ",".join(names) if names else "(none)"
    layers = getattr(bone, "layers", None)
    if layers is not None:
        idxs = [str(i) for i, on in enumerate(layers) if on]
        return "layers:" + (",".join(idxs) if idxs else "(none)")
    return ""


def _dump_armature_detail(L, label, obj):
    """Full per-bone, read-only dump of one armature in rest/armature space.
    Everything an auto-IK solver could key on: order, parent, deform/connect
    flags, length, head/tail (local + world), local axes (roll), child order."""
    L.append("")
    L.append(f"=== FULL BONE DETAIL: {label} ({obj.name if obj else '-'}) ===")
    if obj is None:
        L.append("  (not set)")
        return
    if obj.type != 'ARMATURE':
        L.append(f"  {obj.name}: not an armature — skipped")
        return

    mw = obj.matrix_world
    loc, rot, scale = mw.decompose()
    L.append(f"  matrix_world  loc={_fmt_v(loc)}  scale={_fmt_v(scale)}")
    L.append(f"  matrix_world  rot(quat WXYZ)=({rot.w:+.4f}, {rot.x:+.4f}, {rot.y:+.4f}, {rot.z:+.4f})")
    non_uniform = abs(scale.x - scale.y) > 1e-5 or abs(scale.x - scale.z) > 1e-5
    if abs(scale.x - 1.0) > 1e-5 or non_uniform:
        L.append(f"  NOTE: object scale is not identity (non-uniform={non_uniform}). "
                 f"Resonite bakes world transforms — this affects every bone.")
    L.append(f"  data.pose_position: {obj.data.pose_position}")

    bones = list(obj.data.bones)
    idx_of = {b.name: i for i, b in enumerate(bones)}
    L.append(f"  Bone count: {len(bones)}  (table is in data.bones / export order)")
    L.append("")
    for i, b in enumerate(bones):
        x, y, z = _bone_local_axes(b)
        pname = b.parent.name if b.parent else "-"
        pidx = idx_of.get(b.parent.name, "-") if b.parent else "-"
        L.append(f"  [{i:3d}] {b.name}")
        L.append(f"        parent={pname}[{pidx}]  deform={int(b.use_deform)}  "
                 f"connect={int(b.use_connect)}  inherit_rot={int(b.use_inherit_rotation)}  "
                 f"childN={len(b.children)}  descN={len(b.children_recursive)}")
        L.append(f"        head_local={_fmt_v(b.head_local)}  tail_local={_fmt_v(b.tail_local)}  "
                 f"len={b.length:.5f}")
        L.append(f"        world_head={_fmt_v(mw @ b.head_local)}  world_tail={_fmt_v(mw @ b.tail_local)}")
        L.append(f"        axisX={_fmt_v(x)}  axisY(dir)={_fmt_v(y)}  axisZ={_fmt_v(z)}")
        cols = _bone_collections_str(b)
        if cols:
            L.append(f"        collections={cols}")

    L.append("")
    L.append("  -- child order (parent -> [children, in data order]) --")
    any_multi = False
    for b in bones:
        if b.children:
            kids = [c.name for c in b.children]
            flag = "  <-- multiple children" if len(kids) > 1 else ""
            L.append(f"    {b.name} -> {kids}{flag}")
            if len(kids) > 1:
                any_multi = True
    if not any_multi:
        L.append("    (no bone has more than one child)")


def _dump_limb_focus(L, label, obj):
    """For each detected limb-tip bone (hand/foot/toe/head), print its parent and
    ALL of that parent's children with the geometry an auto-IK solver compares
    when choosing the tip: length, descendant count, direction, world tail.
    Side-by-side with the same dump from a working model, the bone the solver
    would pick (longest / furthest-extending child) becomes obvious."""
    if obj is None or obj.type != 'ARMATURE':
        return
    mw = obj.matrix_world
    targets = [b for b in obj.data.bones
               if any(k in b.name.lower() for k in _LIMB_END_KEYWORDS)]
    if not targets:
        return
    L.append("")
    L.append(f"=== LIMB END-EFFECTOR FOCUS: {label} ({obj.name}) ===")
    L.append("  For each limb-tip parent, the children an auto-IK solver picks between.")
    L.append("  A solver that ignores names tends to pick the longest / furthest-")
    L.append("  extending child as the tip — compare these rows against the working model.")
    seen_parents = set()
    for t in targets:
        parent = t.parent
        if parent is None or parent.name in seen_parents:
            continue
        seen_parents.add(parent.name)
        L.append("")
        L.append(f"  Parent: {parent.name}  len={parent.length:.5f}  dir={_fmt_v(_bone_local_axes(parent)[1], 3)}")
        ranked = sorted(parent.children, key=lambda c: c.length, reverse=True)
        for c in ranked:
            d = (c.tail_local - c.head_local)
            dn = d.normalized() if d.length > 1e-9 else d
            is_end = any(k in c.name.lower() for k in _LIMB_END_KEYWORDS)
            mark = "  <-- limb-tip name" if is_end else ""
            L.append(f"    {c.name:<30} len={c.length:.5f}  descN={len(c.children_recursive)}  "
                     f"deform={int(c.use_deform)}  dir={_fmt_v(dn, 3)}  "
                     f"world_tail={_fmt_v(mw @ c.tail_local, 3)}{mark}")
        longest = ranked[0] if ranked else None
        if longest is not None and not any(k in longest.name.lower() for k in _LIMB_END_KEYWORDS):
            L.append(f"    !! longest child is '{longest.name}', NOT a limb-tip-named bone — "
                     f"a length-based solver would pick it as the tip.")


def _angle_deg(a, b):
    """Angle in degrees between two vectors. 0 if either is degenerate."""
    la, lb = a.length, b.length
    if la < 1e-9 or lb < 1e-9:
        return 0.0
    c = max(-1.0, min(1.0, a.dot(b) / (la * lb)))
    return math.degrees(math.acos(c))


def _dump_ab_diff(L, base, cloth):
    """Bone-by-bone A/B diff between two armatures matched by name. A = base
    (reference / known-good), B = cloth (comparison / suspect). Prints ONLY the
    bones whose IK-relevant attributes differ, with both values, plus a roll-up
    of any limb-tip parents whose 'longest child' identity flips between the two.

    Intended workflow: load the working model as Base and the broken model as
    Clothing in one scene, then dump — the result is a direct 'what changed' report
    instead of a manual diff across two files."""
    if (base is None or cloth is None or base.type != 'ARMATURE'
            or cloth.type != 'ARMATURE' or base == cloth):
        return
    L.append("")
    L.append("=== A/B BONE DIFF (A=BASE reference vs B=CLOTH comparison) ===")
    L.append(f"  A = {base.name}   B = {cloth.name}")
    L.append("  Only bones that differ are listed. Tolerances: len 5e-4, "
             "angle 1.0°, pos 5e-4.")

    a_bones = {b.name: b for b in base.data.bones}
    b_bones = {b.name: b for b in cloth.data.bones}
    a_names, b_names = set(a_bones), set(b_bones)
    shared = sorted(a_names & b_names)

    LEN_TOL, ANG_TOL, POS_TOL = 5e-4, 1.0, 5e-4
    diff_count = 0
    for name in shared:
        a, b = a_bones[name], b_bones[name]
        ax, ay, az = _bone_local_axes(a)
        bx, by, bz = _bone_local_axes(b)
        msgs = []
        if abs(a.length - b.length) > LEN_TOL:
            msgs.append(f"len {a.length:.5f} -> {b.length:.5f} "
                        f"({(b.length - a.length):+.5f})")
        dir_ang = _angle_deg(ay, by)
        if dir_ang > ANG_TOL:
            msgs.append(f"direction {dir_ang:.2f}° off")
        roll_ang = _angle_deg(ax, bx)
        if roll_ang > ANG_TOL:
            msgs.append(f"roll {roll_ang:.2f}° off")
        if (a.head_local - b.head_local).length > POS_TOL:
            msgs.append(f"head {_fmt_v(a.head_local, 4)} -> {_fmt_v(b.head_local, 4)}")
        if (a.tail_local - b.tail_local).length > POS_TOL:
            msgs.append(f"tail {_fmt_v(a.tail_local, 4)} -> {_fmt_v(b.tail_local, 4)}")
        if a.use_deform != b.use_deform:
            msgs.append(f"deform {int(a.use_deform)} -> {int(b.use_deform)}")
        if a.use_connect != b.use_connect:
            msgs.append(f"connect {int(a.use_connect)} -> {int(b.use_connect)}")
        if a.use_inherit_rotation != b.use_inherit_rotation:
            msgs.append(f"inherit_rot {int(a.use_inherit_rotation)} -> {int(b.use_inherit_rotation)}")
        ap = a.parent.name if a.parent else "-"
        bp = b.parent.name if b.parent else "-"
        if ap != bp:
            msgs.append(f"parent {ap} -> {bp}")
        a_kids = [c.name for c in a.children]
        b_kids = [c.name for c in b.children]
        if a_kids != b_kids:
            msgs.append(f"child order {a_kids} -> {b_kids}")
        a_col = _bone_collections_str(a)
        b_col = _bone_collections_str(b)
        if a_col != b_col:
            msgs.append(f"collections {a_col} -> {b_col}")
        if msgs:
            diff_count += 1
            L.append(f"  ~ {name}")
            for m in msgs:
                L.append(f"        {m}")

    only_a = sorted(a_names - b_names)
    only_b = sorted(b_names - a_names)
    L.append("")
    L.append(f"  Differing shared bones: {diff_count} / {len(shared)}")
    L.append(f"  Only in A (base): {len(only_a)} {only_a[:40]}")
    L.append(f"  Only in B (cloth): {len(only_b)} {only_b[:40]}")

    # Did the 'longest child' of any limb-tip parent flip between A and B? That is
    # the exact failure mode for a length-based auto-IK solver.
    L.append("")
    L.append("  -- limb-tip 'longest child' comparison --")
    flips = 0
    a_parents = {bn.parent.name for bn in base.data.bones
                 if bn.parent and any(k in bn.name.lower() for k in _LIMB_END_KEYWORDS)}
    for pname in sorted(a_parents):
        pa, pb = a_bones.get(pname), b_bones.get(pname)
        if pa is None or pb is None or not pa.children or not pb.children:
            continue
        la = max(pa.children, key=lambda c: c.length)
        lb = max(pb.children, key=lambda c: c.length)
        a_is_tip = any(k in la.name.lower() for k in _LIMB_END_KEYWORDS)
        b_is_tip = any(k in lb.name.lower() for k in _LIMB_END_KEYWORDS)
        flag = ""
        if la.name != lb.name or a_is_tip != b_is_tip:
            flag = "  <-- FLIP"
            flips += 1
        L.append(f"    under {pname}: A longest={la.name}({la.length:.4f}) "
                 f"B longest={lb.name}({lb.length:.4f}){flag}")
    if flips:
        L.append(f"  !! {flips} limb-tip parent(s) changed which child is longest — "
                 f"prime suspect for the auto-IK mis-pick.")


def _mesh_arm_targets(mesh):
    return [m.object for m in mesh.modifiers if m.type == 'ARMATURE' and m.object]


def _weight_stats(mesh):
    """Single pass over a mesh's vertices: per vertex-group, how many vertices
    carry a non-trivial weight and the total weight. Lets us spot vertex groups
    that exist but are effectively empty (a bone the mesh declares but doesn't
    actually skin to)."""
    name_by_idx = {vg.index: vg.name for vg in mesh.vertex_groups}
    counts, sums = {}, {}
    for v in mesh.data.vertices:
        for ge in v.groups:
            if ge.weight > 1e-4:
                counts[ge.group] = counts.get(ge.group, 0) + 1
                sums[ge.group] = sums.get(ge.group, 0.0) + ge.weight
    return name_by_idx, counts, sums


def _dump_mesh_weights(L, context, armature):
    """Per-mesh skin diagnostics + a limb-tip weight focus. Resonite builds its
    humanoid/IK rig from the skinned mesh's bone bindings, so a bone that looks
    correct in the armature but carries NO mesh weight (or whose wrist verts were
    moved onto a sibling support bone) will be mis-detected. This surfaces that."""
    meshes = [o for o in context.scene.objects if o.type == 'MESH']
    L.append("")
    L.append("=== MESH SKIN / VERTEX-WEIGHT DIAGNOSTICS ===")
    if not meshes:
        L.append("  (no mesh objects in scene)")
        return

    global_bone = {}  # bone name -> [vert_count, weight_sum]
    for mesh in meshes:
        name_by_idx, counts, sums = _weight_stats(mesh)
        arm = _mesh_arm_targets(mesh)
        empty = [name_by_idx[vg.index] for vg in mesh.vertex_groups
                 if counts.get(vg.index, 0) == 0]
        L.append("")
        L.append(f"  Mesh: {mesh.name}  verts={len(mesh.data.vertices)}  "
                 f"vgroups={len(mesh.vertex_groups)}  "
                 f"armature_mod={[a.name for a in arm]}")
        L.append(f"    empty vgroups ({len(empty)}): {sorted(empty)[:40]}")
        for gi, cnt in counts.items():
            nm = name_by_idx.get(gi)
            if nm is None:
                continue
            gb = global_bone.setdefault(nm, [0, 0.0])
            gb[0] += cnt
            gb[1] += sums.get(gi, 0.0)

    if armature is None or armature.type != 'ARMATURE':
        return
    L.append("")
    L.append("  -- limb-tip weight focus (verts / weight per child, across all meshes) --")
    L.append("  A limb-tip bone (Hand/Foot) with NO WEIGHT while its support sibling")
    L.append("  has weight is the prime cause of an auto-IK solver picking the support.")
    seen = set()
    for b in armature.data.bones:
        if not any(k in b.name.lower() for k in _LIMB_END_KEYWORDS):
            continue
        p = b.parent
        if p is None or p.name in seen:
            continue
        seen.add(p.name)
        L.append(f"    under {p.name}:")
        for c in p.children:
            gb = global_bone.get(c.name, [0, 0.0])
            tip = "  <-- limb-tip name" if any(k in c.name.lower() for k in _LIMB_END_KEYWORDS) else ""
            flag = "  [NO WEIGHT]" if gb[0] == 0 else ""
            L.append(f"      {c.name:<30} verts={gb[0]:6d}  weight_sum={gb[1]:9.2f}{tip}{flag}")


def _detail_targets(context, p):
    """Armatures to run the full-detail / limb-focus dump on. Prefers the two
    configured armatures; if neither is set (e.g. you're inspecting a finished,
    single-armature model where the cloth rig was already deleted), falls back to
    the active object, then to every armature in the scene."""
    out = []
    for obj in (p.base_armature, p.cloth_armature):
        if obj is not None and obj.type == 'ARMATURE' and obj not in out:
            out.append(obj)
    if out:
        return out
    active = context.active_object
    if active is not None and active.type == 'ARMATURE':
        return [active]
    return [o for o in context.scene.objects if o.type == 'ARMATURE']


class CLOTH_TRANSFER_OT_dump_debug(Operator):
    bl_idname = "cloth_transfer.dump_debug"
    bl_label = "Dump Debug Info"
    bl_description = (
        "Collect detailed diagnostic info about both armatures and their "
        "bones. Writes the result to a text block named 'ClothTransfer_Debug' "
        "(open it in the Text Editor to copy/share) and prints to the system "
        "console"
    )
    bl_options = {'INTERNAL'}

    def execute(self, context):
        p = context.scene.cloth_transfer_props
        base, cloth = p.base_armature, p.cloth_armature

        L = []
        L.append("=== Dalek Cloth Transfer — Debug Dump ===")
        L.append(f"Plugin version: {__version_str__}")
        L.append(f"Blender: {bpy.app.version_string}")
        blend_path = bpy.data.filepath
        L.append(f"Blend file: {os.path.basename(blend_path) if blend_path else '(unsaved)'}")
        if blend_path:
            L.append(f"Blend path: {blend_path}")
        L.append(f"Transfer new bones: {p.transfer_new_bones}")
        L.append(f"Delete extra bones: {p.delete_extra_bones}")
        L.append(f"Skip body-part bones: {p.skip_body_part_bones}")
        pb_has = 'select' in bpy.types.PoseBone.bl_rna.properties
        b_has = 'select' in bpy.types.Bone.bl_rna.properties
        L.append(f"Bone-API probe: PoseBone.select={'Y' if pb_has else 'N'}, Bone.select={'Y' if b_has else 'N'}")

        for label, obj in (("BASE", base), ("CLOTH", cloth)):
            L.append("")
            L.append(f"--- {label} ---")
            if obj is None:
                L.append("  (not set)")
                continue
            L.append(f"  Name: {obj.name}")
            L.append(f"  Type: {obj.type}")
            L.append(f"  Location: ({obj.location.x:.4f}, {obj.location.y:.4f}, {obj.location.z:.4f})")
            L.append(f"  Rotation (euler XYZ): ({obj.rotation_euler.x:.4f}, {obj.rotation_euler.y:.4f}, {obj.rotation_euler.z:.4f})")
            L.append(f"  Scale: ({obj.scale.x:.4f}, {obj.scale.y:.4f}, {obj.scale.z:.4f})")
            if obj.type != 'ARMATURE':
                L.append("  (not an armature — rest of dump skipped)")
                continue
            L.append(f"  Pose position: {obj.data.pose_position}")
            L.append(f"  Bone count: {len(obj.data.bones)}")
            names = [b.name for b in obj.data.bones]
            L.append(f"  Bones (first 40): {names[:40]}")
            cons = []
            for pb in obj.pose.bones:
                for c in pb.constraints:
                    cons.append(f"{pb.name}->{c.type}")
            L.append(f"  Pose constraints ({len(cons)}): {cons[:20]}")

        if (base and cloth and base.type == 'ARMATURE' and cloth.type == 'ARMATURE'):
            base_names = {b.name for b in base.data.bones}
            cloth_names = {b.name for b in cloth.data.bones}
            shared = base_names & cloth_names
            only_cloth = cloth_names - base_names
            only_base = base_names - cloth_names

            L.append("")
            L.append("--- BONE COMPARISON ---")
            L.append(f"  Shared:      {len(shared):4d}  ({len(shared) / max(len(base_names), 1) * 100:.1f}% of base)")
            L.append(f"  Only-cloth:  {len(only_cloth):4d}")
            L.append(f"  Only-base:   {len(only_base):4d}")

            shared_sorted = sorted(shared)
            L.append(f"  Shared bone names: {shared_sorted}")
            L.append(f"  Only-cloth (first 40): {sorted(only_cloth)[:40]}")
            L.append(f"  Only-base  (first 40): {sorted(only_base)[:40]}")

            if len(shared) >= 2:
                base_pts = [base.matrix_world @ base.data.bones[n].head_local for n in shared_sorted]
                cloth_pts = [cloth.matrix_world @ cloth.data.bones[n].head_local for n in shared_sorted]

                L.append("")
                L.append("--- SHARED BONE WORLD POSITIONS (rest) ---")
                L.append(f"  {'Bone':<28} {'Base (x,y,z)':>26}  {'Cloth (x,y,z)':>26}  {'Δ':>8}")
                for i, n in enumerate(shared_sorted[:40]):
                    bp = base_pts[i]
                    cp = cloth_pts[i]
                    d = (bp - cp).length
                    L.append(
                        f"  {n:<28} ({bp.x:>7.3f},{bp.y:>7.3f},{bp.z:>7.3f})  "
                        f"({cp.x:>7.3f},{cp.y:>7.3f},{cp.z:>7.3f})  {d:>8.4f}"
                    )

                s, t = _robust_alignment(base_pts, cloth_pts)
                L.append("")
                L.append("--- PROPOSED ALIGNMENT (median estimator) ---")
                L.append(f"  Scale:       ×{s:.6f}")
                L.append(f"  Translation: ({t.x:+.4f}, {t.y:+.4f}, {t.z:+.4f})")

                L.append("")
                L.append("  Per-bone residual after applying the alignment:")
                L.append(f"  {'Bone':<28} {'Residual':>10}")
                residuals = []
                for i, n in enumerate(shared_sorted[:40]):
                    aligned = s * cloth_pts[i] + t
                    d = (base_pts[i] - aligned).length
                    residuals.append(d)
                    L.append(f"  {n:<28} {d:>10.4f}")
                if residuals:
                    L.append(f"  Min/Median/Max residual: {min(residuals):.4f} / "
                             f"{sorted(residuals)[len(residuals)//2]:.4f} / {max(residuals):.4f}")

                ratios = _pairwise_distance_ratios(base_pts, cloth_pts)
                if ratios:
                    ratios.sort()
                    L.append("")
                    L.append("--- PAIRWISE DISTANCE RATIOS (base/cloth) ---")
                    L.append(f"  Pairs evaluated: {len(ratios)}")
                    L.append(f"  Min:    {ratios[0]:.4f}")
                    L.append(f"  P25:    {ratios[len(ratios)//4]:.4f}")
                    L.append(f"  Median: {ratios[len(ratios)//2]:.4f}")
                    L.append(f"  P75:    {ratios[(len(ratios)*3)//4]:.4f}")
                    L.append(f"  Max:    {ratios[-1]:.4f}")
                    L.append(f"  Mean:   {sum(ratios) / len(ratios):.4f}")

        # Full per-bone detail + limb end-effector focus. Runs on the configured
        # armatures, or falls back to the active / all scene armatures so it works
        # on a finished single-armature model (cloth rig already deleted) too.
        targets = _detail_targets(context, p)
        if not targets:
            L.append("")
            L.append("=== FULL BONE DETAIL ===")
            L.append("  (no armature found — set Base/Clothing, select an armature, "
                     "or add one to the scene)")
        else:
            configured = {o for o in (base, cloth) if o is not None}
            for obj in targets:
                if obj is base:
                    label = "BASE"
                elif obj is cloth:
                    label = "CLOTH"
                elif obj in configured:
                    label = "CONFIGURED"
                else:
                    label = "SCENE ARMATURE"
                _dump_armature_detail(L, label, obj)
            for obj in targets:
                label = ("BASE" if obj is base else "CLOTH" if obj is cloth
                         else "ARMATURE")
                _dump_limb_focus(L, label, obj)

        # Direct A/B diff when both armatures are set — load the good model as
        # Base and the broken one as Clothing to get a 'what changed' report.
        _dump_ab_diff(L, base, cloth)

        # Mesh skin / weight diagnostics — the armature can look correct while the
        # skinned mesh (what Resonite actually reads) has lost weight on a limb tip.
        _dump_mesh_weights(L, context, targets[0] if targets else None)

        text = "\n".join(L)

        text_name = "ClothTransfer_Debug"
        tb = bpy.data.texts.get(text_name) or bpy.data.texts.new(text_name)
        tb.clear()
        tb.write(text)
        print(text)

        try:
            context.window_manager.clipboard = text
            clip_ok = True
        except Exception:
            clip_ok = False

        blend_dir = bpy.path.abspath("//") if bpy.data.filepath else ""
        target_dir = blend_dir if blend_dir and os.path.isdir(blend_dir) else tempfile.gettempdir()
        file_path = os.path.join(target_dir, "ClothTransfer_Debug.txt")
        try:
            with open(file_path, "w", encoding="utf-8") as fh:
                fh.write(text)
            file_ok = True
        except Exception as e:
            file_path = str(e)
            file_ok = False

        msg = "Debug info"
        msg += " | clipboard ✓" if clip_ok else " | clipboard ✗"
        msg += f" | file: {file_path}" if file_ok else f" | file failed ({file_path})"
        self.report({'INFO'}, msg)
        return {'FINISHED'}


def _select_bones_in_armature(context, armature, names):
    _ensure_object_mode(context)
    _activate(context, armature)
    _set_mode_on(context, armature, 'POSE')
    with context.temp_override(active_object=armature, object=armature):
        bpy.ops.pose.select_all(action='DESELECT')
    selected = 0
    for n in names:
        pb = armature.pose.bones.get(n)
        if pb is None:
            continue
        if _BLENDER_5:
            pb.select = True
        else:
            pb.bone.select = True
        selected += 1
    return selected


def _sync_pose_selection(armature, coll):
    """Push the enabled-state of `coll` into `armature`'s pose-mode selection,
    but only for bones that appear in the list. Bones outside the list are
    left untouched. No-op when the armature is not currently in pose mode —
    we never drag the user into pose mode on a checkbox toggle."""
    if armature is None or armature.type != 'ARMATURE' or armature.mode != 'POSE':
        return
    pose_bones = armature.pose.bones
    for it in coll:
        pb = pose_bones.get(it.name)
        if pb is None:
            continue
        target = it.enabled
        if _BLENDER_5:
            if pb.select != target:
                pb.select = target
        else:
            if pb.bone.select != target:
                pb.bone.select = target


class CLOTH_TRANSFER_OT_highlight_new_bones(Operator):
    bl_idname = "cloth_transfer.highlight_new_bones"
    bl_label = "Highlight bones to transfer"
    bl_description = (
        "Switch to the clothing armature in pose mode and select the bones that "
        "exist on the clothing armature but not on the base — exactly the bones "
        "that 'Transfer new bones' would add"
    )
    bl_options = {'INTERNAL'}

    @classmethod
    def poll(cls, context):
        p = context.scene.cloth_transfer_props
        return p.base_armature and p.cloth_armature and p.base_armature != p.cloth_armature

    def execute(self, context):
        p = context.scene.cloth_transfer_props
        base, cloth = p.base_armature, p.cloth_armature
        if len(p.transfer_bone_list) > 0:
            names = [it.name for it in p.transfer_bone_list if it.enabled]
            if not names:
                self.report({'INFO'}, "All new bones are unchecked — nothing to highlight")
                return {'CANCELLED'}
        else:
            shared = _shared_bone_names(base, cloth)
            names = [b.name for b in cloth.data.bones if b.name not in shared]
            if not names:
                self.report({'INFO'}, "No new bones — clothing has no bones absent from base")
                return {'CANCELLED'}
        n = _select_bones_in_armature(context, cloth, names)
        self.report({'INFO'}, f"Selected {n} new bones in {cloth.name}")
        return {'FINISHED'}


class CLOTH_TRANSFER_OT_highlight_extra_bones(Operator):
    bl_idname = "cloth_transfer.highlight_extra_bones"
    bl_label = "Highlight bones to delete"
    bl_description = (
        "Switch to the base armature in pose mode and select the bones that "
        "exist on the base armature but not on the clothing — exactly the bones "
        "that 'Delete extra bones from base' would remove. WARNING: deleting "
        "these will break deformation on any base mesh that depends on them"
    )
    bl_options = {'INTERNAL'}

    @classmethod
    def poll(cls, context):
        p = context.scene.cloth_transfer_props
        return p.base_armature and p.cloth_armature and p.base_armature != p.cloth_armature

    def execute(self, context):
        p = context.scene.cloth_transfer_props
        base, cloth = p.base_armature, p.cloth_armature
        if len(p.delete_bone_list) > 0:
            names = [it.name for it in p.delete_bone_list if it.enabled]
            if not names:
                self.report({'INFO'}, "All extra bones are unchecked — nothing to highlight")
                return {'CANCELLED'}
        else:
            shared = _shared_bone_names(base, cloth)
            names = [b.name for b in base.data.bones if b.name not in shared]
            if not names:
                self.report({'INFO'}, "No extra bones — base has no bones absent from clothing")
                return {'CANCELLED'}
        n = _select_bones_in_armature(context, base, names)
        self.report({'INFO'}, f"Selected {n} extra bones in {base.name}")
        return {'FINISHED'}


class CLOTH_TRANSFER_UL_bone_list(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row = layout.row(align=True)
        for _ in range(item.depth):
            row.label(text="", icon='BLANK1')
        row.prop(item, "enabled", text=item.name)


class CLOTH_TRANSFER_OT_refresh_bone_lists(Operator):
    bl_idname = "cloth_transfer.refresh_bone_lists"
    bl_label = "Refresh bone lists"
    bl_description = (
        "Rebuild the per-bone include/exclude lists from the current armatures. "
        "Existing checkbox choices are preserved for bones that still appear"
    )
    bl_options = {'INTERNAL'}

    def execute(self, context):
        p = context.scene.cloth_transfer_props
        _refresh_transfer_bone_list(p)
        _refresh_delete_bone_list(p)
        _sync_pose_selection(p.cloth_armature, p.transfer_bone_list)
        _sync_pose_selection(p.base_armature, p.delete_bone_list)
        return {'FINISHED'}


class CLOTH_TRANSFER_OT_rescan_parent_overrides(Operator):
    bl_idname = "cloth_transfer.rescan_parent_overrides"
    bl_label = "Rescan parent suggestions"
    bl_description = (
        "Recompute fuzzy parent suggestions for cloth-only bones whose parent "
        "name does not exist on the base. Replaces existing picks"
    )
    bl_options = {'INTERNAL'}

    def execute(self, context):
        p = context.scene.cloth_transfer_props
        p.parent_overrides.clear()
        _, total = _sync_parent_overrides(p)
        self.report({'INFO'}, f"Found {total} unmapped parent(s)")
        return {'FINISHED'}


class CLOTH_TRANSFER_OT_metric_info(Operator):
    """No-op operator used as a tooltip carrier on the Compatibility metric
    rows. Drawn with emboss=False so it looks like a label; Blender pulls the
    tooltip text from the dynamic `description` classmethod below."""
    bl_idname = "cloth_transfer.metric_info"
    bl_label = ""
    bl_options = {'INTERNAL'}

    metric: StringProperty()

    _DESCRIPTIONS = {
        'match': (
            "Percentage of base armature bones whose name also exists on the "
            "clothing armature. These shared bones drive pose alignment and "
            "mesh skinning — higher means a closer rig match"
        ),
        'shared': (
            "Bones present in both armatures (matched by name). Pose alignment "
            "uses these, and clothing meshes become rigged to the base copy "
            "of each one after transfer"
        ),
        'only_cloth': (
            "Bones on the clothing armature but not the base. These are the "
            "candidates the 'Transfer new bones' option would copy onto the "
            "base armature"
        ),
        'only_base': (
            "Bones on the base armature but not the clothing. These are the "
            "candidates the 'Delete extra bones from base' option would "
            "remove from the base armature"
        ),
    }

    @classmethod
    def description(cls, context, properties):
        return cls._DESCRIPTIONS.get(properties.metric, "")

    def execute(self, context):
        return {'CANCELLED'}


class CLOTH_TRANSFER_OT_set_all_bones(Operator):
    bl_idname = "cloth_transfer.set_all_bones"
    bl_label = "Set all bones"
    bl_description = "Check or uncheck every bone in the list"
    bl_options = {'INTERNAL'}

    list_kind: StringProperty()
    value: BoolProperty()

    def execute(self, context):
        p = context.scene.cloth_transfer_props
        if self.list_kind == "transfer":
            coll, armature = p.transfer_bone_list, p.cloth_armature
        else:
            coll, armature = p.delete_bone_list, p.base_armature
        _CASCADE_GUARD["depth"] += 1
        try:
            for it in coll:
                if it.enabled != self.value:
                    it.enabled = self.value
        finally:
            _CASCADE_GUARD["depth"] -= 1
        _sync_pose_selection(armature, coll)
        return {'FINISHED'}


def _cloth_has_dependents(cloth):
    if cloth is None:
        return True
    for o in bpy.data.objects:
        if o.type == 'MESH':
            if o.parent == cloth:
                return True
            if any(m.type == 'ARMATURE' and m.object == cloth for m in o.modifiers):
                return True
    return False


class CLOTH_TRANSFER_OT_delete_cloth_armature(Operator):
    bl_idname = "cloth_transfer.delete_cloth_armature"
    bl_label = "Delete Clothing Armature"
    bl_description = (
        "Delete the source clothing armature object and its data. Only enabled "
        "after Transfer Clothing has run — i.e., when no mesh still references "
        "the clothing armature as parent or as an armature-modifier target"
    )
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        p = context.scene.cloth_transfer_props
        return p.cloth_armature is not None and not _cloth_has_dependents(p.cloth_armature)

    def execute(self, context):
        p = context.scene.cloth_transfer_props
        cloth = p.cloth_armature
        if cloth is None:
            self.report({'ERROR'}, "No clothing armature selected")
            return {'CANCELLED'}
        if _cloth_has_dependents(cloth):
            self.report(
                {'ERROR'},
                "Clothing armature still has mesh dependents — run Transfer Clothing first",
            )
            return {'CANCELLED'}

        cloth_name = cloth.name
        arm_data = cloth.data
        p.cloth_armature = None
        bpy.data.objects.remove(cloth, do_unlink=True)
        if arm_data.users == 0:
            bpy.data.armatures.remove(arm_data)

        self.report({'INFO'}, f"Deleted clothing armature: {cloth_name}")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

class CLOTH_TRANSFER_PT_panel(Panel):
    bl_label = "Cloth Transfer"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Cloth Transfer"

    def draw(self, context):
        layout = self.layout
        p = context.scene.cloth_transfer_props

        box = layout.box()
        box.label(text="Armatures", icon='ARMATURE_DATA')
        box.prop(p, "base_armature", text="Base")
        box.prop(p, "cloth_armature", text="Clothing")

        box = layout.box()
        box.label(text="Compatibility", icon='CHECKMARK')
        self._draw_compat_metrics(box, p)

        box = layout.box()
        box.label(text="Options", icon='PREFERENCES')
        box.prop(p, "skip_body_part_bones")
        row = box.row(align=True)
        row.prop(p, "transfer_new_bones")
        row.operator(
            "cloth_transfer.highlight_new_bones",
            text="",
            icon='RESTRICT_SELECT_OFF',
        )
        if p.transfer_new_bones:
            self._draw_bone_list(
                box,
                p,
                "transfer_bone_list",
                "transfer_bone_active_index",
                "transfer",
                "Bones to transfer",
            )
            if len(p.parent_overrides) > 0:
                self._draw_parent_overrides(box, p)
        row = box.row(align=True)
        row.prop(p, "delete_extra_bones")
        row.operator(
            "cloth_transfer.highlight_extra_bones",
            text="",
            icon='RESTRICT_SELECT_OFF',
        )
        if p.delete_extra_bones:
            self._draw_bone_list(
                box,
                p,
                "delete_bone_list",
                "delete_bone_active_index",
                "delete",
                "Bones to delete",
            )

        col = layout.column(align=True)
        col.scale_y = 1.1
        col.operator("cloth_transfer.match_pose", icon='POSE_HLT')

        row = layout.row()
        row.scale_y = 1.6
        row.operator("cloth_transfer.transfer", icon='OUTLINER_OB_ARMATURE')

        layout.operator("cloth_transfer.delete_cloth_armature", icon='TRASH')

        box = layout.box()
        box.label(text="Debug", icon='CONSOLE')
        box.operator("cloth_transfer.dump_debug", icon='TEXT')

    def _draw_compat_metrics(self, parent, p):
        base = p.base_armature
        cloth = p.cloth_armature
        if (
            base is None or cloth is None or base == cloth
            or base.type != 'ARMATURE' or cloth.type != 'ARMATURE'
        ):
            parent.label(text="Pick both armatures to see metrics", icon='INFO')
            return
        base_names = {b.name for b in base.data.bones}
        cloth_names = {b.name for b in cloth.data.bones}
        shared = base_names & cloth_names
        ratio = len(shared) / max(len(base_names), 1)
        col = parent.column(align=True)
        rows = (
            ('match', f"Match: {ratio * 100:.0f}%"),
            ('shared', f"Shared: {len(shared)}"),
            ('only_cloth', f"Only-cloth: {len(cloth_names - base_names)}"),
            ('only_base', f"Only-base: {len(base_names - cloth_names)}"),
        )
        for key, text in rows:
            op = col.operator("cloth_transfer.metric_info", text=text, emboss=False)
            op.metric = key

    def _draw_bone_list(self, parent, p, list_attr, idx_attr, kind, header_text):
        coll = getattr(p, list_attr)
        expanded_attr = f"{kind}_list_expanded"
        expanded = getattr(p, expanded_attr)
        sub = parent.box()
        head = sub.row(align=True)
        head.prop(
            p,
            expanded_attr,
            text="",
            icon='TRIA_DOWN' if expanded else 'TRIA_RIGHT',
            emboss=False,
        )
        enabled_count = sum(1 for it in coll if it.enabled)
        head.label(text=f"{header_text} ({enabled_count}/{len(coll)})")
        op_all = head.operator("cloth_transfer.set_all_bones", text="All")
        op_all.list_kind = kind
        op_all.value = True
        op_none = head.operator("cloth_transfer.set_all_bones", text="None")
        op_none.list_kind = kind
        op_none.value = False
        head.operator("cloth_transfer.refresh_bone_lists", text="", icon='FILE_REFRESH')
        if not expanded:
            return
        if len(coll) == 0:
            sub.label(text="(no bones)", icon='INFO')
            return
        sub.template_list(
            "CLOTH_TRANSFER_UL_bone_list",
            kind,
            p, list_attr,
            p, idx_attr,
            rows=8,
        )

    def _draw_parent_overrides(self, parent, p):
        expanded = p.parent_overrides_expanded
        sub = parent.box()
        head = sub.row(align=True)
        head.prop(
            p,
            "parent_overrides_expanded",
            text="",
            icon='TRIA_DOWN' if expanded else 'TRIA_RIGHT',
            emboss=False,
        )
        head.label(text=f"Unmapped parents ({len(p.parent_overrides)})", icon='ERROR')
        head.operator("cloth_transfer.rescan_parent_overrides", text="", icon='FILE_REFRESH')
        if not expanded:
            return
        sub.label(text="Pick a base bone for each, or leave blank for root", icon='INFO')
        base_data = (
            p.base_armature.data
            if p.base_armature is not None and p.base_armature.type == 'ARMATURE'
            else None
        )
        for item in p.parent_overrides:
            row = sub.row(align=True)
            row.label(text=item.bone_name, icon='BONE_DATA')
            row.label(text=f"was: {item.original_parent}")
            if base_data is not None:
                row.prop_search(item, "chosen_parent", base_data, "bones", text="")
            else:
                row.prop(item, "chosen_parent", text="")


# ---------------------------------------------------------------------------
# register
# ---------------------------------------------------------------------------

_classes = (
    ClothTransferBoneItem,
    ClothTransferParentOverride,
    ClothTransferProps,
    CLOTH_TRANSFER_OT_match_pose,
    CLOTH_TRANSFER_OT_transfer,
    CLOTH_TRANSFER_OT_highlight_new_bones,
    CLOTH_TRANSFER_OT_highlight_extra_bones,
    CLOTH_TRANSFER_OT_refresh_bone_lists,
    CLOTH_TRANSFER_OT_rescan_parent_overrides,
    CLOTH_TRANSFER_OT_metric_info,
    CLOTH_TRANSFER_OT_set_all_bones,
    CLOTH_TRANSFER_OT_delete_cloth_armature,
    CLOTH_TRANSFER_OT_dump_debug,
    CLOTH_TRANSFER_UL_bone_list,
    CLOTH_TRANSFER_PT_panel,
)


def register():
    for c in _classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.cloth_transfer_props = PointerProperty(type=ClothTransferProps)


def unregister():
    if hasattr(bpy.types.Scene, "cloth_transfer_props"):
        del bpy.types.Scene.cloth_transfer_props
    for c in reversed(_classes):
        bpy.utils.unregister_class(c)


if __name__ == "__main__":
    register()
