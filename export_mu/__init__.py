# vim:ts=4:et
# ##### BEGIN GPL LICENSE BLOCK #####
#
#  This program is free software; you can redistribute it and/or
#  modify it under the terms of the GNU General Public License
#  as published by the Free Software Foundation; either version 2
#  of the License, or (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program; if not, write to the Free Software Foundation,
#  Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301, USA.
#
# ##### END GPL LICENSE BLOCK #####

# <pep8 compliant>

import os

import bpy, bmesh
from bpy_extras.object_utils import object_data_add
from mathutils import Vector,Matrix,Quaternion
from pprint import pprint
from math import pi
from bpy_extras.io_utils import ExportHelper
from bpy.props import StringProperty

from ..mu import MuEnum, Mu, MuColliderMesh, MuColliderSphere, MuColliderCapsule
from ..mu import MuObject, MuTransform, MuMesh, MuTagLayer, MuRenderer, MuLight
from ..mu import MuCamera
from ..mu import MuColliderBox, MuColliderWheel, MuMaterial, MuTexture, MuMatTex
from ..mu import MuSpring, MuFriction
from ..mu import MuAnimation, MuClip, MuCurve, MuKey
from ..shader import make_shader
from .. import properties
from ..cfgnode import ConfigNode, ConfigNodeError
from ..parser import parse_node
from ..attachnode import AttachNode
from ..utils import strip_nnn, swapyz, swizzleq, vector_str
from ..volume import model_volume

from .mesh import make_mesh
from .collider import make_collider
from .animation import collect_animations, find_path_root, make_animations

def make_transform(obj):
    transform = MuTransform()
    transform.name = strip_nnn(obj.name)
    transform.localPosition = obj.location
    if obj.rotation_mode != 'QUATERNION':
      transform.localRotation = obj.rotation_euler.to_quaternion()
    else:
      transform.localRotation = obj.rotation_quaternion
    transform.localScale = obj.scale
    return transform

def make_tag_and_layer(obj):
    tl = MuTagLayer()
    tl.tag = obj.muproperties.tag
    tl.layer = obj.muproperties.layer
    return tl

def make_texture(mu, tex):
    if tex.tex not in mu.textures:
        mutex = MuTexture()
        mutex.name = tex.tex
        mutex.type = tex.type
        mutex.index = len(mu.textures)
        mu.textures[tex.tex] = mutex
    mattex = MuMatTex()
    mattex.index = mu.textures[tex.tex].index
    mattex.scale = list(tex.scale)
    mattex.offset = list(tex.offset)
    return mattex

def make_property(blendprop):
    muprop = {}
    for item in blendprop:
        if type(item.value) is float:
            muprop[item.name] = item.value
        else:
            muprop[item.name] = list(item.value)
    return muprop

def make_tex_property(mu, blendprop):
    muprop = {}
    for item in blendprop:
        muprop[item.name] = make_texture(mu, item)
    return muprop

def make_material(mu, mat):
    material = MuMaterial()
    material.name = mat.name
    material.index = len(mu.materials)
    matprops = mat.mumatprop
    material.shaderName = matprops.shaderName
    material.colorProperties = make_property(matprops.color.properties)
    material.vectorProperties = make_property(matprops.vector.properties)
    material.floatProperties2 = make_property(matprops.float2.properties)
    material.floatProperties3 = make_property(matprops.float3.properties)
    material.textureProperties = make_tex_property(mu, matprops.texture.properties)
    return material

def make_renderer(mu, mesh):
    rend = MuRenderer()
    #FIXME shadows
    rend.materials = []
    for mat in mesh.materials:
        if mat.mumatprop.shaderName:
            if mat.name not in mu.materials:
                mu.materials[mat.name] = make_material(mu, mat)
            rend.materials.append(mu.materials[mat.name].index)
    if not rend.materials:
        return None
    return rend

def make_light(mu, light, obj):
    mulight = MuLight()
    mulight.type = ('SPOT', 'SUN', 'POINT', 'AREA').index(light.type)
    mulight.color = tuple(light.color) + (1.0,)
    mulight.range = light.distance
    mulight.intensity = light.energy
    mulight.spotAngle = 0.0
    mulight.cullingMask = properties.GetPropMask(obj.muproperties.cullingMask)
    if light.type == 'SPOT':
        mulight.spotAngle = light.spot_size * 180 / pi
    return mulight

def make_camera(mu, camera, obj):
    mucamera = MuCamera()
    clear = obj.muproperties.clearFlags
    flags = ('SKYBOX', 'COLOR', 'DEPTH', 'NOTHING').index(clear)
    mucamera.clearFlags = flags + 1
    mucamera.backgroundColor = obj.muproperties.backgroundColor
    mucamera.cullingMask = properties.GetPropMask(obj.muproperties.cullingMask)
    mucamera.orthographic = camera.type == 'ORTHO'
    mucamera.fov = camera.angle * 180 / pi
    mucamera.near = camera.clip_start
    mucamera.far = camera.clip_end
    mucamera.depth = obj.muproperties.depth
    return mucamera

light_types = {
    bpy.types.PointLight,
    bpy.types.SunLight,
    bpy.types.SpotLight,
    bpy.types.HemiLight,
    bpy.types.AreaLight
}

exportable_types = {bpy.types.Mesh, bpy.types.Camera} | light_types

def is_group_root(obj, group):
    print(obj.name)
    while obj.parent:
        obj = obj.parent
        print(obj.name, obj.users_group)
        if group.name not in obj.users_group:
            return False
    return True

def make_obj(mu, obj, special, path = ""):
    muobj = MuObject()
    muobj.transform = make_transform (obj)
    if path:
        path += "/"
    path += muobj.transform.name
    mu.object_paths[path] = muobj
    muobj.tag_and_layer = make_tag_and_layer(obj)
    if not obj.data :
        name = strip_nnn(obj.name)
        if name[:5] == "node_":
            n = AttachNode(obj, mu.inverse)
            mu.nodes.append(n)
            if not n.keep_transform():
                return None
            # Blender's empties use the +Z axis for single-arrow display, so
            # that is the most natural orientation for nodes in blender.
            # However, KSP uses the transform's +Z (Unity) axis which is
            # Blender's +Y, so rotate 90 degrees around local X to go from
            # Blender to KSP
            rot = Quaternion((0.5**0.5,0.5**0.5,0,0))
            muobj.transform.localRotation = muobj.transform.localRotation @ rot
        elif name in ["CoMOffset", "CoPOffset", "CoLOffset"]:
            setattr(mu, name, (mu.inverse @ obj.matrix_world.col[3])[:3])
        pass
    if not obj.data and obj.dupli_group:
        group = obj.dupli_group
        for o in group.objects:
            # while KSP models (part/prop/internal) will have only one root
            # object, grouping might be used for other purposes (eg, greeble)
            # so support multiple group root objects
            if not is_group_root(o, group):
                continue
            #easiest way to deal with dupli_offset is to temporarily shift
            #the object by the offset and then restor the object's location
            loc = o.location
            o.location -= group.dupli_offset
            child = make_obj(mu, o, special, path)
            o.location = loc
            if child:
                muobj.children.append(child)
    elif obj.muproperties.collider and obj.muproperties.collider != 'MU_COL_NONE':
        # colliders are children of the object representing the transform so
        # they are never exported directly.
        pass
    elif obj.data:
        if type(obj.data) == bpy.types.Mesh:
            muobj.shared_mesh = make_mesh(mu, obj)
            muobj.renderer = make_renderer(mu, obj.data)
        elif type(obj.data) in light_types:
            muobj.light = make_light(mu, obj.data, obj)
            # Blender points spotlights along local -Z, unity along local +Z
            # which is Blender's +Y, so rotate -90 degrees around local X to
            # go from Blender to Unity
            rot = Quaternion((0.5**0.5,-0.5**0.5,0,0))
            muobj.transform.localRotation = muobj.transform.localRotation @ rot
        elif type(obj.data) == bpy.types.Camera:
            muobj.camera = make_camera(mu, obj.data, obj)
            # Blender points camera along local -Z, unity along local +Z
            # which is Blender's +Y, so rotate -90 degrees around local X to
            # go from Blender to Unity
            rot = Quaternion((0.5**0.5,-0.5**0.5,0,0))
            muobj.transform.localRotation = muobj.transform.localRotation @ rot
    for o in obj.children:
        muprops = o.muproperties
        if muprops.modelType in special:
            if special[muprops.modelType](mu, o):
                continue
        if muprops.collider and muprops.collider != 'MU_COL_NONE':
            muobj.collider = make_collider(mu, o)
            continue
        if (o.data and type(o.data) not in exportable_types):
            continue
        child = make_obj(mu, o, special, path)
        if child:
            muobj.children.append(child)
    return muobj

def find_template(mu, filepath):
    base = os.path.splitext(filepath)
    cfg = base[0] + ".cfg"

    cfgin = mu.name + ".cfg.in"
    if cfgin in bpy.data.texts:
        return cfg, ConfigNode.load(bpy.data.texts[cfgin].as_string())

    cfgin = base[0] + ".cfg.in"
    if os.path.isfile (cfgin):
        try:
            return cfg, ConfigNode.loadfile(cfgin)
        except ConfigNodeError as e:
            print("Error reading", cfgin, e.message)

    return None, None

def add_internal_node(node, internal):
    # NOTE this assumes the internal is the direct child of the part's root
    # also, it assumes the internal is correctly oriented relative to the part
    # (FIXME?)
    inode = node.AddNewNode('INTERNAL')
    inode.AddValue("name", strip_nnn(internal.name))
    if internal.location:
        inode.AddValue("offset", vector_str(swapyz(internal.location)))
    # not really a good idea IMHO, but it's there...
    if internal.scale != Vector((1, 1, 1)):
        inode.AddValue("scale", vector_str(swapyz(internal.scale)))

def add_prop_node(node, prop):
    # NOTE this assumes the prop is the direct child of the internal's root
    pnode = node.AddNewNode('PROP')
    pnode.AddValue("name", strip_nnn(prop.name))
    pnode.AddValue("position", vector_str(swapyz(prop.location)))
    pnode.AddValue("position", vector_str(swizzleq(prop.rotation_quaternion)))
    pnode.AddValue("scale", vector_str(swapyz(prop.scale)))

def generate_cfg(mu, filepath):
    cfgfile, cfgnode = find_template(mu, filepath)
    if not cfgnode:
        return
    ntype = mu.type
    if ntype == 'NONE':
        ntype = bpy.context.scene.musceneprops.modelType
    node = cfgnode.GetNode(ntype)
    if not node:
        return
    parse_node(mu, cfgnode)
    if ntype == 'PART':
        if mu.CoMOffset != None:
            node.AddValue("CoMOffset", vector_str(swapyz(mu.CoMOffset)))
        if mu.CoPOffset != None:
            node.AddValue("CoPOffset", vector_str(swapyz(mu.CoPOffset)))
        if mu.CoLOffset != None:
            node.AddValue("CoLOffset", vector_str(swapyz(mu.CoLOffset)))
        if mu.internal:
            add_internal_node(node, mu.internal)
        mu.nodes.sort()
        for n in mu.nodes:
            n.save(node)
    elif ntype == 'INTERNAL':
        for prop in mu.props:
            add_prop_node(node, prop)
    # nothing meaningful for PROP
    of = open(cfgfile, "wt")
    for n in cfgnode.nodes:
        of.write(n[0] + " " + n[1].ToString())

def add_internal(mu, obj):
    if not mu.internal:
        mu.internal = obj
    return True

def add_prop(mu, obj):
    mu.props.append(obj)
    return True

special_modelTypes = {
    'NONE': {},
    'PART': {'INTERNAL':add_internal},
    'PROP': {},
    'INTERNAL': {'PROP':add_prop},
}

def export_object(obj, filepath):
    animations = collect_animations(obj)
    anim_root = find_path_root(animations)
    mu = Mu()
    mu.name = strip_nnn(obj.name)
    mu.object_paths = {}
    mu.materials = {}
    mu.textures = {}
    mu.nodes = []
    mu.props = []
    mu.internal = None
    mu.type = obj.muproperties.modelType
    mu.CoMOffset = None
    mu.CoPOffset = None
    mu.CoLOffset = None
    mu.inverse = obj.matrix_world.inverted()
    mu.obj = make_obj(mu, obj, special_modelTypes[mu.type])
    mu.materials = list(mu.materials.values())
    mu.materials.sort(key=lambda x: x.index)
    mu.textures = list(mu.textures.values())
    mu.textures.sort(key=lambda x: x.index)
    if anim_root:
        anim_root_obj = mu.object_paths[anim_root]
        anim_root_obj.animation = make_animations(mu, animations, anim_root)
    mu.write(filepath)
    mu.skin_volume, mu.ext_volume = model_volume(obj)
    generate_cfg(mu, filepath)
    return mu

def export_mu(operator, context, filepath):
    export_object (context.active_object, filepath)
    return {'FINISHED'}

class KSPMU_OT_ExportMu(bpy.types.Operator, ExportHelper):
    '''Save a KSP Mu (.mu) File'''
    bl_idname = "export_object.ksp_mu"
    bl_label = "Export Mu"

    filename_ext = ".mu"
    filter_glob: StringProperty(default="*.mu", options={'HIDDEN'})

    @classmethod
    def poll(cls, context):
        return (context.active_object != None
                and (not context.active_object.data
                     or type(context.active_object.data) == bpy.types.Mesh))

    def execute(self, context):
        keywords = self.as_keywords (ignore=("check_existing", "filter_glob"))
        return export_mu(self, context, **keywords)

class KSPMU_OT_ExportMu_quick(bpy.types.Operator, ExportHelper):
    '''Save a KSP Mu (.mu) File, defaulting name to selected object'''
    bl_idname = "export_object.ksp_mu_quick"
    bl_label = "Export Mu (quick)"

    filename_ext = ".mu"
    filter_glob: StringProperty(default="*.mu", options={'HIDDEN'})

    @classmethod
    def poll(cls, context):
        return (context.active_object != None
                and (not context.active_object.data
                     or type(context.active_object.data) == bpy.types.Mesh))

    def execute(self, context):
        keywords = self.as_keywords (ignore=("check_existing", "filter_glob"))
        return export_mu(self, context, **keywords)

    def invoke(self, context, event):
        if context.active_object != None:
            self.filepath = strip_nnn(context.active_object.name) + self.filename_ext
        return ExportHelper.invoke(self, context, event)

class WORKSPACE_PT_tools_mu_export(bpy.types.Panel):
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_category = "Mu Tools"
    bl_context = ".workspace"
    bl_label = "Export Mu"

    def draw(self, context):
        layout = self.layout
        #col = layout.column(align=True)
        layout.operator("export_object.ksp_mu_quick", text = "Export Mu Model");
        layout.operator("object.mu_volume", text = "Calc Mu Volume");

def export_mu_menu_func(self, context):
    self.layout.operator(KSPMU_OT_ExportMu.bl_idname, text="KSP Mu (.mu)")

classes = (
    KSPMU_OT_ExportMu,
    KSPMU_OT_ExportMu_quick,
    WORKSPACE_PT_tools_mu_export,
)

menus = (
    (bpy.types.TOPBAR_MT_file_export, export_mu_menu_func),
)