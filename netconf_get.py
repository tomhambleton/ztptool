#!/usr/bin/env python3
"""Connect to a NETCONF server and retrieve the full running datastore."""

import argparse
import sys
from ncclient import manager
from lxml import etree


def get_full_tree(host, port, username, password, hostkey_verify=False):
    with manager.connect(
        host=host,
        port=port,
        username=username,
        password=password,
        hostkey_verify=hostkey_verify,
    ) as m:
        response = m.get()
        return response.data_xml


def main():
    parser = argparse.ArgumentParser(description="NETCONF get on entire tree")
    parser.add_argument("host", help="NETCONF server hostname or IP")
    parser.add_argument("--port", type=int, default=830)
    parser.add_argument("--username", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--hostkey-verify", action="store_true", default=False)
    parser.add_argument("--output", help="Write XML output to file instead of stdout")
    args = parser.parse_args()

    xml_bytes = get_full_tree(
        host=args.host,
        port=args.port,
        username=args.username,
        password=args.password,
        hostkey_verify=args.hostkey_verify,
    )

    pretty = etree.tostring(
        etree.fromstring(xml_bytes.encode() if isinstance(xml_bytes, str) else xml_bytes),
        pretty_print=True,
    )

    if args.output:
        with open(args.output, "wb") as f:
            f.write(pretty)
        print(f"Output written to {args.output}")
    else:
        sys.stdout.buffer.write(pretty)


if __name__ == "__main__":
    main()
