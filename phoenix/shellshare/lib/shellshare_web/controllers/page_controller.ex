defmodule ShellshareWeb.PageController do
  use ShellshareWeb, :controller

  def home(conn, _params) do
    render(conn, :home, layout: false)
  end
end
