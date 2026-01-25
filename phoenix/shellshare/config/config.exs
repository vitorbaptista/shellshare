# This file is responsible for configuring your application
# and its dependencies with the aid of the Config module.
import Config

# Configures the endpoint
config :shellshare, ShellshareWeb.Endpoint,
  url: [host: "localhost"],
  adapter: Bandit.PhoenixAdapter,
  render_errors: [
    formats: [html: ShellshareWeb.ErrorHTML, json: ShellshareWeb.ErrorJSON],
    layout: false
  ],
  pubsub_server: Shellshare.PubSub,
  live_view: [signing_salt: "shellshare_lv_salt"]

# Configure esbuild (the version is required)
config :esbuild,
  version: "0.17.11",
  shellshare: [
    args:
      ~w(js/app.js --bundle --target=es2017 --outdir=../priv/static/assets --external:/fonts/* --external:/images/*),
    cd: Path.expand("../assets", __DIR__),
    env: %{"NODE_PATH" => Path.expand("../deps", __DIR__)}
  ]

# Configure tailwind (the version is required)
config :tailwind,
  version: "3.4.0",
  shellshare: [
    args: ~w(
      --config=tailwind.config.js
      --input=css/app.css
      --output=../priv/static/assets/app.css
    ),
    cd: Path.expand("../assets", __DIR__)
  ]

# Configures Elixir's Logger
config :logger, :console,
  format: "$time $metadata[$level] $message\n",
  metadata: [:request_id]

# Use Jason for JSON parsing in Phoenix
config :phoenix, :json_library, Jason

# Room configuration
config :shellshare, :room,
  # Time-to-live for inactive rooms (5 minutes)
  ttl_ms: 5 * 60 * 1000,
  # Maximum buffer size per room (1MB)
  max_buffer_size: 1024 * 1024

# Import environment specific config. This must remain at the bottom
# of this file so it overrides the configuration defined above.
import_config "#{config_env()}.exs"
