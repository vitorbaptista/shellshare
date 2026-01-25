defmodule ShellshareWeb.Router do
  use ShellshareWeb, :router

  pipeline :browser do
    plug :accepts, ["html"]
    plug :fetch_session
    plug :fetch_live_flash
    plug :put_root_layout, html: {ShellshareWeb.Layouts, :root}
    plug :protect_from_forgery
    plug :put_secure_browser_headers
  end

  pipeline :api do
    plug :accepts, ["json"]
  end

  scope "/", ShellshareWeb do
    pipe_through :browser

    get "/", PageController, :home
  end

  # Room routes - API for Python client
  scope "/r", ShellshareWeb do
    pipe_through :api

    post "/:room", RoomController, :push
    delete "/:room", RoomController, :delete
  end

  # Room viewer - LiveView
  scope "/r", ShellshareWeb do
    pipe_through :browser

    live "/:room", RoomLive, :show
  end
end
