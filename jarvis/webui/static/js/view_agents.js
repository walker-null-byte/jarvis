// Agents view: #/agents is the grid (agent_grid.js), #/agents/<id> the workspace (agent_workspace.js).
import { registerView } from "./views.js";
import { mountGrid } from "./agent_grid.js";
import { mountWorkspace } from "./agent_workspace.js";
import { mountBuilder } from "./agent_builder.js";

let current = null;

registerView("agents", {
  mount(container, params, ctx) {
    current = params.build ? mountBuilder(container, params.build, ctx) : params.id ? mountWorkspace(container, params.id, ctx) : mountGrid(container, ctx);
  },
  unmount() {
    current?.unmount?.();
    current = null;
  },
});
