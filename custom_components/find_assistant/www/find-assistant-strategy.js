// Find Assistant -- Lovelace strategy.
//
// Groups every Find Assistant tracker by its current room and, per tracker,
// shows a "Last Seen" row plus a "Ring" button (when the device has one --
// only FMDN devices do). Trackers that have never been seen at all (their
// _last_seen sensor is still "unknown"/"unavailable") are skipped entirely,
// since there's nothing meaningful to show for them yet.
//
// Trackers currently away get their own section instead of a room, with an
// extra "Last Known Location" row (Google Maps link, FMDN devices only) and
// a shared map card plotting every away tracker that has coordinates --
// semantic reports (e.g. "Home") have no lat/long and just won't get a pin.
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
  // only) a ring button and a _last_known_location sensor, all sharing
  // the same device_id.
  const byDevice = {};
  for (const entityId of Object.keys(entities)) {
    const entry = entities[entityId];
    if (entry.platform !== "find_assistant" || !entry.device_id) continue;
    (byDevice[entry.device_id] = byDevice[entry.device_id] || []).push(entityId);
  }

  const rooms = {};

  for (const deviceId of Object.keys(byDevice)) {
    const entityIds = byDevice[deviceId];
    const roomEntityId = entityIds.find(
      (id) => id.startsWith("sensor.") && id.endsWith("_room")
    );
    const lastSeenEntityId = entityIds.find((id) => id.endsWith("_last_seen"));
    const ringEntityId = entityIds.find(
      (id) => id.startsWith("button.") && id.endsWith("_ring")
    );
    const lastKnownLocationEntityId = entityIds.find((id) =>
      id.endsWith("_last_known_location")
    );
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
      lastKnownLocationEntityId: isAway ? lastKnownLocationEntityId : undefined,
    });
  }

  const roomNames = Object.keys(rooms)
    .filter((name) => name !== awayTitle)
    .sort((a, b) => a.localeCompare(b));
  if (rooms[awayTitle]) roomNames.push(awayTitle);

  const cards = [];

  roomNames.forEach((roomName) => {
    const trackers = rooms[roomName].slice().sort((a, b) => a.name.localeCompare(b.name));

    // Shared map for the Away section, plotting every away tracker that
    // has a geo (lat/long) location report -- the map card silently skips
    // any entity without those attributes, so semantic-only reports
    // (e.g. "Home") just don't get a pin rather than causing an error.
    if (roomName === awayTitle) {
      const mapEntities = trackers
        .map((t) => t.lastKnownLocationEntityId)
        .filter(Boolean);
      if (mapEntities.length > 0) {
        cards.push({ type: "map", entities: mapEntities });
      }
    }

    const rows = [];
    trackers.forEach((tracker) => {
      rows.push({ type: "section", label: tracker.name });
      rows.push({ entity: tracker.lastSeenEntityId, name: "Last Seen" });
      if (tracker.ringEntityId) {
        rows.push({ entity: tracker.ringEntityId, name: "Ring" });
      }
      if (tracker.lastKnownLocationEntityId) {
        rows.push({ entity: tracker.lastKnownLocationEntityId, name: "Last Known Location" });
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
