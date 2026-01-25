import Config

# config/runtime.exs is executed for all environments, including
# during releases. It is executed after compilation and before the
# temporary directory (if present) is cleaned up.

if config_env() == :prod do
  secret_key_base =
    System.get_env("SECRET_KEY_BASE") ||
      raise """
      environment variable SECRET_KEY_BASE is missing.
      You can generate one by calling: mix phx.gen.secret
      """

  host = System.get_env("PHX_HOST") || "shellshare.net"
  port = String.to_integer(System.get_env("PORT") || "4000")

  config :shellshare, :dns_cluster_query, System.get_env("DNS_CLUSTER_QUERY")

  config :shellshare, ShellshareWeb.Endpoint,
    url: [host: host, port: 443, scheme: "https"],
    http: [
      ip: {0, 0, 0, 0, 0, 0, 0, 0},
      port: port
    ],
    secret_key_base: secret_key_base

  # Room configuration from environment
  if ttl = System.get_env("ROOM_TTL_MS") do
    config :shellshare, :room,
      ttl_ms: String.to_integer(ttl)
  end
end
