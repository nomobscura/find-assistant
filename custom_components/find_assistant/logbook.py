"""
Teaches HA's Logbook how to describe our enter/leave events (see
const.EVENT_TAG_ENTERED_ROOM / EVENT_TAG_LEFT_ROOM, fired from
presence.py's _fire_transition_events) as readable "<tag> entered/left
<room>" activity, rather than raw event-data dumps. Since each event
carries the tag's own entity_id, these show up in that entity's/device's
own Logbook tab, not just a global feed -- directly addressing "add
activity ... for when a tag enters and leaves the proxy (device)".
"""
from collections.abc import Callable

from homeassistant.components.logbook import (
    LOGBOOK_ENTRY_ENTITY_ID,
    LOGBOOK_ENTRY_MESSAGE,
    LOGBOOK_ENTRY_NAME,
)
from homeassistant.core import Event, HomeAssistant, callback

from .const import DOMAIN, EVENT_TAG_ENTERED_ROOM, EVENT_TAG_LEFT_ROOM, EVENT_TAG_SPOTTED_BY_PROXY


@callback
def async_describe_events(
    hass: HomeAssistant,
    async_describe_event: Callable[[str, str, Callable[[Event], dict]], None],
) -> None:
    @callback
    def _describe_entered(event: Event) -> dict:
        data = event.data
        return {
            LOGBOOK_ENTRY_NAME: data.get("name"),
            LOGBOOK_ENTRY_MESSAGE: f"entered {data.get('room')}",
            LOGBOOK_ENTRY_ENTITY_ID: data.get("entity_id"),
        }

    @callback
    def _describe_left(event: Event) -> dict:
        data = event.data
        return {
            LOGBOOK_ENTRY_NAME: data.get("name"),
            LOGBOOK_ENTRY_MESSAGE: f"left {data.get('room')}",
            LOGBOOK_ENTRY_ENTITY_ID: data.get("entity_id"),
        }

    @callback
    def _describe_spotted(event: Event) -> dict:
        # This event's device_id targets the PROXY, not the tag -- so unlike
        # the two above, LOGBOOK_ENTRY_NAME here is the *tag's* name (the
        # subject of the sentence), read from the Proxy's own Logbook tab:
        # "<tag name> was first spotted nearby".
        data = event.data
        return {
            LOGBOOK_ENTRY_NAME: data.get("name"),
            LOGBOOK_ENTRY_MESSAGE: "was first spotted nearby",
            LOGBOOK_ENTRY_ENTITY_ID: data.get("tag_entity_id"),
        }

    async_describe_event(DOMAIN, EVENT_TAG_ENTERED_ROOM, _describe_entered)
    async_describe_event(DOMAIN, EVENT_TAG_LEFT_ROOM, _describe_left)
    async_describe_event(DOMAIN, EVENT_TAG_SPOTTED_BY_PROXY, _describe_spotted)
