defmodule ShellshareWeb.RoomController do
  @moduledoc """
  HTTP API controller for the Python shellshare client.

  Handles:
  - POST /r/:room - receive terminal data
  - DELETE /r/:room - cleanup room
  """

  use ShellshareWeb, :controller

  alias Shellshare.Room

  @doc """
  Receives terminal data from the Python client.

  Expects:
  - Authorization header with the room secret
  - JSON body with:
    - message: base64 encoded terminal output
    - size: {cols: int, rows: int}
  """
  def push(conn, %{"room" => room_name} = params) do
    secret = get_authorization(conn)
    message = params["message"]
    size = params["size"]

    if is_nil(secret) or secret == "" do
      send_resp(conn, 401, "Unauthorized")
    else
      case Room.get_or_create(room_name, secret) do
        {:ok, pid} ->
          Room.push(pid, %{"message" => message, "size" => size})
          send_resp(conn, 200, "OK")

        {:error, :unauthorized} ->
          send_resp(conn, 401, "Unauthorized")
      end
    end
  end

  @doc """
  Deletes/closes a room.

  Requires Authorization header matching the room's secret.
  """
  def delete(conn, %{"room" => room_name}) do
    secret = get_authorization(conn)

    case Room.lookup(room_name) do
      {:ok, pid} ->
        if Room.authorized?(pid, secret) do
          Room.stop(pid)
          send_resp(conn, 202, "Accepted")
        else
          send_resp(conn, 401, "Unauthorized")
        end

      {:error, :not_found} ->
        # Room doesn't exist, that's fine
        send_resp(conn, 202, "Accepted")
    end
  end

  defp get_authorization(conn) do
    case get_req_header(conn, "authorization") do
      [secret | _] -> secret
      [] -> nil
    end
  end
end
