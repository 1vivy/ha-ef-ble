#!/usr/bin/env python3
"""Expose EcoFlow BLE Home Assistant entities as an SNMP v2c agent."""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

_LOGGER = logging.getLogger(__name__)

UPS_MIB_OIDS = {
    "battery_charge": "1.3.6.1.2.1.33.1.2.4.0",  # upsEstimatedChargeRemaining
    "battery_runtime": "1.3.6.1.2.1.33.1.2.3.0",  # upsEstimatedMinutesRemaining
    "input_voltage": "1.3.6.1.2.1.33.1.3.3.1.3.1",  # upsInputVoltage.1
    "output_voltage": "1.3.6.1.2.1.33.1.4.4.1.2.1",  # upsOutputVoltage.1
    "output_current": "1.3.6.1.2.1.33.1.4.4.1.3.1",  # upsOutputCurrent.1
    "output_load": "1.3.6.1.2.1.33.1.4.4.1.5.1",  # upsOutputPercentLoad.1
}


@dataclass
class BridgeState:
    enabled: bool = True
    battery_charge: int = 0
    battery_runtime: int = 0
    input_voltage: int = 0
    output_voltage: int = 0
    output_current: int = 0
    output_load: int = 0


class HomeAssistantClient:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token

    def get_state(self, entity_id: str) -> str:
        request = urllib.request.Request(
            f"{self.base_url}/api/states/{entity_id}",
            headers={"Authorization": f"Bearer {self.token}"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
            return str(payload["state"])


def _to_int(value: str, scale: float = 1.0) -> int:
    return max(int(round(float(value) * scale)), 0)


def _is_enabled(value: str) -> bool:
    return value.lower() in {"on", "true", "1", "home", "open"}


def _fill_entity_defaults(args: argparse.Namespace) -> None:
    if not args.entity_prefix:
        return

    defaults = {
        "entity_battery_charge": f"sensor.{args.entity_prefix}_battery_level",
        "entity_battery_runtime": f"sensor.{args.entity_prefix}_battery_runtime",
        "entity_input_voltage": f"sensor.{args.entity_prefix}_ac_input_voltage",
        "entity_output_voltage": f"sensor.{args.entity_prefix}_ac_output_voltage",
        "entity_output_current": f"sensor.{args.entity_prefix}_ac_output_current",
        "entity_output_load": f"sensor.{args.entity_prefix}_ac_output_percent_load",
    }

    for key, value in defaults.items():
        if getattr(args, key) is None:
            setattr(args, key, value)


def update_loop(state: BridgeState, client: HomeAssistantClient, args: argparse.Namespace):
    while True:
        try:
            if args.entity_enabled:
                state.enabled = _is_enabled(client.get_state(args.entity_enabled))

            if state.enabled:
                state.battery_charge = _to_int(client.get_state(args.entity_battery_charge))

                if args.entity_battery_runtime:
                    state.battery_runtime = _to_int(
                        client.get_state(args.entity_battery_runtime),
                    )
                if args.entity_input_voltage:
                    state.input_voltage = _to_int(client.get_state(args.entity_input_voltage))
                if args.entity_output_voltage:
                    state.output_voltage = _to_int(client.get_state(args.entity_output_voltage))
                if args.entity_output_current:
                    state.output_current = _to_int(
                        client.get_state(args.entity_output_current),
                        scale=10,
                    )
                if args.entity_output_load:
                    state.output_load = _to_int(client.get_state(args.entity_output_load))
            else:
                state.battery_charge = 0
                state.battery_runtime = 0
                state.input_voltage = 0
                state.output_voltage = 0
                state.output_current = 0
                state.output_load = 0
        except (ValueError, KeyError, urllib.error.URLError) as err:
            _LOGGER.warning("Polling failed: %s", err)

        time.sleep(args.poll_interval)


def build_agent(bind_host: str, bind_port: int, community: str):
    # Lazy import so --help works even if pysnmp is not installed.
    udp = importlib.import_module("pysnmp.carrier.asyncore.dgram.udp")
    config = importlib.import_module("pysnmp.entity.config")
    engine = importlib.import_module("pysnmp.entity.engine")
    cmdrsp = importlib.import_module("pysnmp.entity.rfc3413.cmdrsp")
    instrum = importlib.import_module("pysnmp.smi.instrum")
    rfc1902 = importlib.import_module("pysnmp.smi.rfc1902")

    snmp_engine = engine.SnmpEngine()
    config.addTransport(
        snmp_engine,
        udp.domainName,
        udp.UdpTransport().openServerMode((bind_host, bind_port)),
    )
    config.addV1System(snmp_engine, "ecoflow-v2", community)
    config.addVacmUser(snmp_engine, 2, "ecoflow-v2", "noAuthNoPriv", (1, 3, 6))

    mib_builder = snmp_engine.getMibBuilder()
    mib_instrum = instrum.MibInstrumController(mib_builder)

    cmdrsp.GetCommandResponder(snmp_engine, mib_instrum)
    cmdrsp.NextCommandResponder(snmp_engine, mib_instrum)

    return snmp_engine, mib_instrum, rfc1902


def set_values(mib_instrum, types, state: BridgeState):
    integer = types.Integer
    mib_instrum.writeVars(
        tuple(
            (oid, integer(value))
            for oid, value in (
                (UPS_MIB_OIDS["battery_charge"], state.battery_charge),
                (UPS_MIB_OIDS["battery_runtime"], state.battery_runtime),
                (UPS_MIB_OIDS["input_voltage"], state.input_voltage),
                (UPS_MIB_OIDS["output_voltage"], state.output_voltage),
                (UPS_MIB_OIDS["output_current"], state.output_current),
                (UPS_MIB_OIDS["output_load"], state.output_load),
            )
        )
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ha-url", required=True, help="Home Assistant URL")
    parser.add_argument("--ha-token", required=True, help="Home Assistant long-lived token")
    parser.add_argument("--community", default="public", help="SNMP v2c community")
    parser.add_argument("--bind-host", default="0.0.0.0")
    parser.add_argument("--bind-port", type=int, default=16161)
    parser.add_argument("--poll-interval", type=int, default=10)
    parser.add_argument(
        "--entity-prefix",
        help="Optional shared sensor prefix, e.g. 'delta_3'.",
    )
    parser.add_argument(
        "--entity-enabled",
        help="Optional switch entity used to enable/disable SNMP export (on/off).",
    )
    parser.add_argument("--entity-battery-charge", help="Battery level sensor entity")
    parser.add_argument("--entity-battery-runtime", help="Battery runtime in minutes")
    parser.add_argument("--entity-input-voltage", help="AC input voltage sensor")
    parser.add_argument("--entity-output-voltage", help="AC output voltage sensor")
    parser.add_argument("--entity-output-current", help="AC output current sensor")
    parser.add_argument("--entity-output-load", help="Output load percent sensor")

    args = parser.parse_args()
    _fill_entity_defaults(args)

    if args.entity_battery_charge is None:
        parser.error("--entity-battery-charge is required (or provide --entity-prefix)")

    return args


def main() -> int:
    logging.basicConfig(level=logging.INFO)

    args = parse_args()
    state = BridgeState()
    client = HomeAssistantClient(args.ha_url, args.ha_token)

    thread = threading.Thread(target=update_loop, args=(state, client, args), daemon=True)
    thread.start()

    snmp_engine, mib_instrum, rfc1902 = build_agent(
        args.bind_host, args.bind_port, args.community
    )

    snmp_engine.transportDispatcher.jobStarted(1)
    _LOGGER.info("SNMP agent listening on udp://%s:%s", args.bind_host, args.bind_port)

    try:
        while True:
            set_values(mib_instrum, rfc1902, state)
            snmp_engine.transportDispatcher.runDispatcher(timeout=1.0)
    except KeyboardInterrupt:
        return 0
    finally:
        snmp_engine.transportDispatcher.closeDispatcher()


if __name__ == "__main__":
    raise SystemExit(main())
