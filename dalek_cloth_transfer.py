bl_info = {
    "name": "Dalek Cloth Transfer",
    "author": "Dalek",
    "version": (1, 0, 0),
    "blender": (3, 0, 0),
    "location": "View3D > Sidebar > Cloth Transfer",
    "description": "Transfer Booth-style clothing armatures and meshes onto a base avatar armature",
    "category": "Rigging",
}

__version__ = bl_info["version"]
__version_str__ = ".".join(str(x) for x in __version__)

import difflib
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
        new_mod = mesh.modifiers.new(name="Armature", type='ARMATURE')
        new_mod.object = cloth

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
