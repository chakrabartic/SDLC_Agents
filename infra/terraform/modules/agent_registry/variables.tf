variable "project_id" {
  description = "Gateway project: registry entries and IAM bindings must live in the same project as the Agent Gateway."
  type        = string
}

variable "region" {
  description = "Regional registry paired with the regional Agent Gateway."
  type        = string
}

variable "mcp_servers" {
  description = "MCP servers to register (key = server name; a toolspec file <key>.json must exist in tool_spec_dir)."
  type = map(object({
    url         = string
    description = string
  }))
}

variable "agents" {
  description = "A2A agents reachable through Agent Gateway egress (commit-executor). url = Agent Runtime A2A endpoint."
  type = map(object({
    url              = string
    description      = string
    protocol_binding = optional(string, "JSONRPC")
    agent_card_json  = optional(string) # A2A agent card (adk generate_agent_card); null = NO_SPEC
  }))
  default = {}
}

variable "endpoints" {
  description = "Plain endpoints (essential Google APIs the agents need once bound to an enforcing gateway)."
  type = map(object({
    url         = string
    description = string
  }))
  default = {}
}

variable "tool_spec_dir" {
  description = "Directory with <server>.json MCP tool specs (annotations drive mcp.tool.isReadOnly). Regenerate from the MCP server code in CI."
  type        = string
  default     = null
}
