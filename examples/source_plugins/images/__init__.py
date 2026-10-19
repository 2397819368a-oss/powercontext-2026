"""Server-local PNG/JPEG write capabilities; install powercontext[source-images]."""

from powercontext.source_management import Capability

from .definition import ImageCapture, ImageDefinition

image_definition = ImageDefinition()
definitions = (image_definition,)
capabilities = (
    Capability(
        name="images.write",
        version="1",
        kind="write",
        description="Save a supplied PNG or JPEG image",
        input_schema=ImageCapture.model_json_schema(),
        definition_name=image_definition.name,
        definition_version=image_definition.version,
    ),
)
