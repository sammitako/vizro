from __future__ import annotations

from typing import List, TypedDict

from dash import Input, Output, Patch, callback, dcc, html

try:
    from pydantic.v1 import Field, root_validator, validator
except ImportError:  # pragma: no cov
    from pydantic import Field, root_validator, validator

from vizro._constants import UPDATE_FIGURES_ACTION_PREFIX
from vizro.actions import update_figures
from vizro.managers import model_manager
from vizro.managers._model_manager import DuplicateIDError, ModelID
from vizro.models import Action, Layout, VizroBaseModel
from vizro.models._action._actions_chain import _action_validator_factory
from vizro.models._layout import set_layout
from vizro.models._models_utils import _log_call, _validate_min_length

from .types import ComponentType, ControlType

# This is just used for type checking. Ideally it would inherit from some dash.development.base_component.Component
# (e.g. html.Div) as well as TypedDict, but that's not possible, and Dash does not have typing support anyway. When
# this type is used, the object is actually still a dash.development.base_component.Component, but this makes it easier
# to see what contract the component fulfills by making the expected keys explicit.
_PageBuildType = TypedDict("_PageBuildType", {"control-panel": html.Div, "page-components": html.Div})


class Page(VizroBaseModel):
    """A page in [`Dashboard`][vizro.models.Dashboard] with its own URL path and place in the `Navigation`.

    Args:
        components (List[ComponentType]): See [ComponentType][vizro.models.types.ComponentType]. At least one component
            has to be provided.
        title (str): Title to be displayed.
        description (str): Description for meta tags.
        layout (Layout): Layout to place components in. Defaults to `None`.
        controls (List[ControlType]): See [ControlType][vizro.models.types.ControlType]. Defaults to `[]`.
        path (str): Path to navigate to page. Defaults to `""`.

    """

    components: List[ComponentType]
    title: str = Field(..., description="Title to be displayed.")
    description: str = Field("", description="Description for meta tags.")
    layout: Layout = None  # type: ignore[assignment]
    controls: List[ControlType] = []
    path: str = Field("", description="Path to navigate to page.")

    actions: List[Action] = []

    # Re-used validators
    _validate_components = validator("components", allow_reuse=True, always=True)(_validate_min_length)
    _validate_layout = validator("layout", allow_reuse=True, always=True)(set_layout)
    _set_actions = _action_validator_factory(
        trigger_property="data",
        component_id_prefix=f"{UPDATE_FIGURES_ACTION_PREFIX}_trigger_",
        actions_chain_id=f"{UPDATE_FIGURES_ACTION_PREFIX}_action_chain_id",
    )

    @root_validator(pre=True)
    def set_id(cls, values):
        if "title" not in values:
            return values

        values.setdefault("id", values["title"])
        return values

    @validator("path", always=True)
    def set_path(cls, path, values) -> str:
        # Based on how Github generates anchor links - see:
        # https://stackoverflow.com/questions/72536973/how-are-github-markdown-anchor-links-constructed.
        def clean_path(path, allowed_characters):
            path = path.strip().lower().replace(" ", "-")
            path = "".join(character for character in path if character.isalnum() or character in allowed_characters)
            return "/" + path

        # Allow "/" in path if provided by user, otherwise turn page id into suitable URL path (not allowing "/")
        if path:
            return clean_path(path, "-_/")
        return clean_path(values["id"], "-_")

    def __init__(self, **data):
        """Adds the model instance to the model manager."""
        try:
            super().__init__(**data)
        except DuplicateIDError as exc:
            raise ValueError(
                f"Page with id={self.id} already exists. Page id is automatically set to the same "
                f"as the page title. If you have multiple pages with the same title then you must assign a unique id."
            ) from exc

    @_log_call
    def pre_build(self):
        # TODO-AV2-TICKET-CREATED: Handle overwriting default actions in the same way as in filter/parameter
        targets = model_manager._get_page_model_ids_with_figure(page_id=ModelID(str(self.id)))
        if targets and not self.actions:
            self.actions = [
                Action(
                    id=f"{UPDATE_FIGURES_ACTION_PREFIX}_action_{self.id}",
                    function=update_figures(targets=targets),
                )
            ]

    @_log_call
    def build(self) -> _PageBuildType:
        self._update_graph_theme()
        controls_content = [control.build() for control in self.controls]
        control_panel = html.Div(children=controls_content, id="control-panel", hidden=not controls_content)

        components_container = self.layout.build()
        for component_idx, component in enumerate(self.components):
            components_container[f"{self.layout.id}_{component_idx}"].children = component.build()

        # Page specific CSS ID and Stores
        components_container.children.append(dcc.Store(id=f"{UPDATE_FIGURES_ACTION_PREFIX}_trigger_{self.id}"))
        components_container.id = "page-components"
        return html.Div([control_panel, components_container], id=self.id)

    def _update_graph_theme(self):
        # The obvious way to do this would be to alter pio.templates.default, but this changes global state and so is
        # not good.
        # Putting graphs as inputs here would be a nice way to trigger the theme change automatically so that we don't

        # need the call to _update_theme inside Graph.__call__ also, but this results in an extra callback and the graph
        # flickering.
        # The code is written to be generic and extensible so that it runs _update_theme on any component with such a
        # method defined. But at the moment this just means Graphs.
        # TODO-AV2-TICKET-CREATED-*: consider making this clientside callback and then possibly we can remove the
        #  call to _update_theme in Graph.__call__ without any flickering.
        #  If we do this then we should consider defining the callback in Graph itself rather than at Page
        #  level. This would mean multiple callbacks on one page but if it's clientside that probably doesn't matter.

        themed_components = [
            model_manager[model_id]
            for model_id in model_manager._get_model_children(model_id=ModelID(str(self.id)))
            if hasattr(model_manager[model_id], "_update_theme")
        ]
        if themed_components:

            @callback(
                [Output(component.id, "figure", allow_duplicate=True) for component in themed_components],
                Input("theme_selector", "checked"),
                prevent_initial_call="initial_duplicate",
            )
            def update_graph_theme(theme_selector: bool):
                return [component._update_theme(Patch(), theme_selector) for component in themed_components]
