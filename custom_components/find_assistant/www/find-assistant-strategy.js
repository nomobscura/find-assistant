// Find Assistant -- Lovelace strategy.
//
// Groups every Find Assistant tracker by its current room and, per tracker,
// shows a "Last Location Change" row plus a "Ring" button (when the device
// has one -- only FMDN devices do). Trackers that have never been seen at
// all (their _last_seen sensor -- which tracks room changes, not raw
// sightings -- is still "unknown"/"unavailable") are skipped entirely,
// since there's nothing meaningful to show for them yet. Trackers currently
// away get their own section instead of a room.
//
// (There used to be a "Last Known Location" row + map for away trackers,
// backed by a Google location-report sensor -- removed along with that
// sensor once live testing confirmed it could never populate: getting a
// real location from Google requires a live FCM push connection this
// integration deliberately doesn't have, not something the plain device-list
// sync call returns. See google_findmy/session.py's list_devices() docstring.)
//
// Registers BOTH a view strategy and a dashboard strategy (same underlying
// logic) per HA's Lovelace strategy contract
// (https://developers.home-assistant.io/docs/frontend/custom-ui/custom-strategy/),
// since it's easy to put `strategy:` at the wrong level in a dashboard's raw
// YAML editor -- either usage works:
//
//   # Whole dashboard is generated:
//   strategy:
//     type: custom:find-assistant-trackers
//     title: Trackers        # optional, defaults to "Trackers"
//     away_title: Away       # optional, defaults to "Away"
//
//   # One view within a normal dashboard is generated:
//   views:
//     - strategy:
//         type: custom:find-assistant-trackers
//         title: Trackers
//         away_title: Away

function buildTrackersView(config, hass) {
  const awayTitle = config.away_title || "Away";
  const entities = hass.entities || {};
  const states = hass.states || {};
  const devices = hass.devices || {};

  // Group this integration's entities by device_id first -- each tracked
  // device has a _room sensor, a _last_seen sensor, and (FMDN devices
  // only) a ring button, all sharing the same device_id.
  const byDevice = {};
  for (const entityId of Object.keys(entities)) {
    const entry = entities[entityId];
    if (entry.platform !== "find_assistant" || !entry.device_id) continue;
    (byDevice[entry.device_id] = byDevice[entry.device_id] || []).push(entityId);
  }

  const rooms = {};

  for (const deviceId of Object.keys(byDevice)) {
    const entityIds = byDevice[deviceId];
    // Identified by domain/entity_category/device_class, deliberately NOT by
    // matching an entity_id suffix -- confirmed live that HA appends "_2",
    // "_3", etc. to entity_id whenever an orphaned entity (e.g. left over
    // from an earlier rename) is already squatting the "clean" name, and a
    // suffix-based match then silently fails to find that entity at all.
    // Since only one of each of these exists per device, matching on
    // properties that don't shift with renaming/collisions is both simpler
    // and actually correct.
    //
    // Room/"Location" sensor: the only non-diagnostic sensor this
    // integration creates.
    const roomEntityId = entityIds.find(
      (id) => id.startsWith("sensor.") && !entities[id].entity_category
    );
    // Last Seen/"Last Location Change" sensor: the only TIMESTAMP-class
    // diagnostic sensor this integration creates (rssi/last_location/
    // current_mac/irk are all plain string diagnostics).
    const lastSeenEntityId = entityIds.find((id) => {
      if (!id.startsWith("sensor.") || entities[id].entity_category !== "diagnostic") return false;
      const state = states[id];
      return state && state.attributes && state.attributes.device_class === "timestamp";
    });
    // Ring button: the only button-domain entity this integration creates
    // per device (FMDN devices only) -- domain alone is unambiguous here.
    const ringEntityId = entityIds.find((id) => id.startsWith("button."));
    if (!roomEntityId || !lastSeenEntityId) continue;

    const lastSeenState = states[lastSeenEntityId];
    const neverSeen =
      !lastSeenState ||
      !lastSeenState.state ||
      lastSeenState.state === "unknown" ||
      lastSeenState.state === "unavailable";
    if (neverSeen) continue; // per design: never-seen trackers are ignored

    const roomState = states[roomEntityId];
    const roomValue = roomState ? roomState.state : undefined;
    const isAway =
      !roomValue || roomValue === "not_home" || roomValue === "unknown" || roomValue === "unavailable";
    const currentRoom = isAway ? awayTitle : roomValue;

    const device = devices[deviceId];
    const trackerName = (device && (device.name_by_user || device.name)) || roomEntityId;

    (rooms[currentRoom] = rooms[currentRoom] || []).push({
      name: trackerName,
      lastSeenEntityId,
      ringEntityId,
    });
  }

  const roomNames = Object.keys(rooms)
    .filter((name) => name !== awayTitle)
    .sort((a, b) => a.localeCompare(b));
  if (rooms[awayTitle]) roomNames.push(awayTitle);

  const cards = [];

  roomNames.forEach((roomName) => {
    const trackers = rooms[roomName].slice().sort((a, b) => a.name.localeCompare(b.name));

    const rows = [];
    trackers.forEach((tracker) => {
      rows.push({ type: "section", label: tracker.name });
      rows.push({ entity: tracker.lastSeenEntityId, name: "Last Location Change" });
      if (tracker.ringEntityId) {
        // Deliberately no `name:` override here -- the ring button's own
        // name property toggles between "<device> Ring" and "<device> Stop
        // Ringing" depending on its live state (see button.py). Pinning a
        // static "Ring" label here, as an earlier version of this file did,
        // silently overrides that and makes the row never visibly reflect
        // the toggle even though the icon (not overridden) still did.
        rows.push({ entity: tracker.ringEntityId });
      }
    });
    cards.push({
      type: "entities",
      title: roomName,
      entities: rows,
    });
  });

  if (cards.length === 0) {
    cards.push({
      type: "markdown",
      content: "No Find Assistant trackers have been seen yet.",
    });
  }

  return {
    title: config.title || "Trackers",
    cards,
  };
}

class FindAssistantTrackersViewStrategy extends HTMLElement {
  static async generate(config, hass) {
    return buildTrackersView(config, hass);
  }
}
customElements.define("ll-strategy-view-find-assistant-trackers", FindAssistantTrackersViewStrategy);

class FindAssistantTrackersDashboardStrategy extends HTMLElement {
  static async generate(config, hass) {
    const view = buildTrackersView(config, hass);
    return {
      title: config.title || "Find Assistant",
      views: [{ path: "trackers", ...view }],
    };
  }
}
customElements.define(
  "ll-strategy-dashboard-find-assistant-trackers",
  FindAssistantTrackersDashboardStrategy
);
