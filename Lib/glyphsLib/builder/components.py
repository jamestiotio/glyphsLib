# Copyright 2015 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


import logging

from fontTools.pens.basePen import MissingComponentError
from fontTools.pens.recordingPen import DecomposingRecordingPen
from glyphsLib.classes import GSBackgroundLayer
from glyphsLib.types import Transform

from .smart_components import to_ufo_smart_component
from .constants import GLYPHS_PREFIX, COMPONENT_INFO_KEY, SMART_COMPONENT_AXES_LIB_KEY

logger = logging.getLogger(__name__)


def to_ufo_components(self, ufo_glyph, layer):
    """Draw .glyphs components onto a pen, adding them to the parent glyph."""

    # NOTE: The UFO v3 and Glyphs data model have incompatible component reference
    # semantics. UFO components always point to a glyph in the same layer, Glyphs
    # components in a ...:
    #  - master layer: point to glyphs in the same master layer.
    #  - non-master layer: point to glyphs in the layer with the same name and fall
    #    back to glyphs in the associated master layer
    # There are some valid use-cases for components in non-master layers, and doing it
    # thoroughly correctly is time-consuming, so we're decomposing just the background
    # layer components as a band-aid.
    if layer.components and isinstance(layer, GSBackgroundLayer):
        logger.warning(
            f"Glyph '{ufo_glyph.name}': All components of the background layer of "
            f"'{layer.foreground.name}' will be decomposed."
        )
        to_ufo_components_nonmaster_decompose(self, ufo_glyph, layer)
        return

    pen = ufo_glyph.getPointPen()
    for index, component in enumerate(layer.components):
        component_name = component.name
        if layer._is_color_palette_layer():
            # Glyphs handles components for color layers in a special way. If
            # the component glyph has color layers of its own, the component
            # use the first color layer with the same color index, otherwise it
            # fallback to the default layer. We try to do that here as well.
            font = layer.parent.parent
            component_glyph = font.glyphs[component_name]
            color_layers = [
                l for l in component_glyph.layers if l._is_color_palette_layer()
            ]
            for i, l in enumerate(color_layers):
                if l._color_palette_index() == layer._color_palette_index():
                    if l.layerId != l.associatedMasterId:
                        # If it is not a master layer, we rename it in
                        # _to_ufo_color_palette_layers(), so we reference the
                        # same name here.
                        component_name += f".color{i}"
                    break
        # XXX We may also want to test here if we're compiling a font (and decompose
        # if so) or changing the representation format (in which case we leave it
        # as a component and save the smart component values).
        # See https://github.com/googlefonts/glyphsLib/pull/822
        if component.smartComponentValues and component.component.smartComponentAxes:
            to_ufo_smart_component(self, layer, component, pen)
        else:
            pen.addComponent(component_name, component.transform)

        if not (component.anchor or component.alignment):
            continue

        component_info = {"name": component.name, "index": index}
        if component.anchor:
            component_info["anchor"] = component.anchor
        if component.alignment:
            component_info["alignment"] = component.alignment

        if COMPONENT_INFO_KEY not in ufo_glyph.lib:
            ufo_glyph.lib[COMPONENT_INFO_KEY] = []
        ufo_glyph.lib[COMPONENT_INFO_KEY].append(component_info)

    # data related to components that is not stored in ComponentInfo is
    # stored in lists of booleans. each list's elements correspond to the
    # components in order.
    for key in ["locked", "smartComponentValues"]:
        values = [getattr(c, key) for c in layer.components]
        if any(values):
            ufo_glyph.lib[_lib_key(key)] = values


def to_ufo_components_nonmaster_decompose(self, ufo_glyph, layer):
    """Draw decomposed .glyphs background and non-master layers with a pen,
    adding them to the parent glyph."""

    if isinstance(layer, GSBackgroundLayer):
        layer_id = layer.foreground.layerId
        layer_master_id = layer.foreground.associatedMasterId
    else:
        layer_id = layer.layerId
        layer_master_id = layer.associatedMasterId

    if layer_id in self._glyph_sets:
        layers = self._glyph_sets[layer_id]
    else:
        if layer_id == layer_master_id:
            # Is a master layer.
            layers = self._glyph_sets[layer_id] = {
                g.name: l
                for g in layer.parent.parent.glyphs
                for l in g.layers
                if l.layerId == layer_id
            }
        else:
            # Is a non-master layer.
            layers_nonmaster = {
                g.name: l
                for g in layer.parent.parent.glyphs
                for l in g.layers
                if l.name == layer.name
            }
            layers_master = {
                g.name: l
                for g in layer.parent.parent.glyphs
                for l in g.layers
                if l.layerId == layer_master_id
            }
            layers = self._glyph_sets[layer_id] = {
                **layers_master,
                **layers_nonmaster,
            }

    rpen = DecomposingRecordingPen(glyphSet=layers)
    for component in layer.components:
        try:
            component.draw(rpen)
        except MissingComponentError as e:
            raise MissingComponentError(
                f"Glyph '{ufo_glyph.name}', background layer: component "
                f"'{component.name}' points to a non-existent glyph."
            ) from e
    rpen.replay(ufo_glyph.getPen())


def to_glyphs_components(self, ufo_glyph, layer):
    for comp in ufo_glyph.components:
        component = self.glyphs_module.GSComponent(comp.baseGlyph)
        component.transform = Transform(*comp.transformation)
        layer.components.append(component)

    for key in ["alignment", "locked", "smartComponentValues"]:
        if _lib_key(key) not in ufo_glyph.lib:
            continue
        # FIXME: (jany) move to using component identifiers for robustness
        # "alignment" is read, but not written for source backwards compatibility.
        values = ufo_glyph.lib[_lib_key(key)]
        for component, value in zip(layer.components, values):
            if value is not None:
                setattr(component, key, value)

    if COMPONENT_INFO_KEY in ufo_glyph.lib:
        for index, component_info in enumerate(ufo_glyph.lib[COMPONENT_INFO_KEY]):
            if "index" not in component_info or "name" not in component_info:
                logger.warning(
                    "Glyph %s, layer %s: The ComponentInfo at index %s is missing "
                    "index and/or name keys. Skipping, component properties will be "
                    "lost.",
                    ufo_glyph.name,
                    layer.name,
                    index,
                )
                continue

            component_index = component_info["index"]
            try:
                component = layer.components[component_index]
            except IndexError:
                logger.warning(
                    "Glyph %s, layer %s: The ComponentInfo at index %s is referencing "
                    "a component that does not exist. Skipping, component properties "
                    "will be lost.",
                    ufo_glyph.name,
                    layer.name,
                    index,
                )
                continue

            if component.name == component_info["name"]:
                if "anchor" in component_info:
                    component.anchor = component_info["anchor"]
                if "alignment" in component_info:
                    component.alignment = component_info["alignment"]
            else:
                logger.warning(
                    "Glyph %s, layer %s: The ComponentInfo at index %s says the "
                    "component at index %s is named '%s', but it is actually named "
                    "'%s'. Skipping, component properties will be lost.",
                    ufo_glyph.name,
                    layer.name,
                    index,
                    component_index,
                    component_info["name"],
                    component.name,
                )


def _lib_key(key):
    key = key[0].upper() + key[1:]
    return f"{GLYPHS_PREFIX}components{key}"


AXES_LIB_KEY = GLYPHS_PREFIX + "smartComponentAxes"


def to_ufo_smart_component_axes(self, ufo_glyph, glyph):
    def _to_ufo_axis(axis):
        return {
            "name": axis.name,
            "bottomName": axis.bottomName,
            "bottomValue": axis.bottomValue,
            "topName": axis.topName,
            "topValue": axis.topValue,
        }

    if glyph.smartComponentAxes:
        ufo_glyph.lib[SMART_COMPONENT_AXES_LIB_KEY] = [
            _to_ufo_axis(a) for a in glyph.smartComponentAxes
        ]


def to_glyphs_smart_component_axes(self, ufo_glyph, glyph):
    def _to_glyphs_axis(axis):
        res = self.glyphs_module.GSSmartComponentAxis()
        res.name = axis["name"]
        res.bottomName = axis["bottomName"]
        res.bottomValue = axis["bottomValue"]
        res.topValue = axis["topValue"]
        res.topName = axis["topName"]
        return res

    if SMART_COMPONENT_AXES_LIB_KEY in ufo_glyph.lib:
        glyph.smartComponentAxes = [
            _to_glyphs_axis(a) for a in ufo_glyph.lib[SMART_COMPONENT_AXES_LIB_KEY]
        ]
