#!/usr/bin/env python3
import os
import json
import singer
from singer import metadata, metrics, utils
from singer.catalog import Catalog, CatalogEntry, Schema
from . import streams as streams_
from .client import XeroClient
from .context import Context
from google.cloud import secretmanager

REQUIRED_CONFIG_KEYS = [
    "start_date",
    "client_id",
    "client_secret",
    "tenant_id",
    "refresh_token",

]

LOGGER = singer.get_logger()

BAD_CREDS_MESSAGE = (
    "Failed to refresh OAuth token using the credentials from both the config and S3. "
    "The token might need to be reauthorized from the integration's properties "
    "or there could be another authentication issue. Please attempt to reauthorize "
    "the integration."
)


class BadCredsException(Exception):
    pass


def get_abs_path(path):
    return os.path.join(os.path.dirname(os.path.realpath(__file__)), path)


def load_schema(tap_stream_id):
    path = "schemas/{}.json".format(tap_stream_id)
    schema = utils.load_json(get_abs_path(path))
    dependencies = schema.pop("tap_schema_dependencies", [])
    refs = {}
    for sub_stream_id in dependencies:
        refs[sub_stream_id] = load_schema(sub_stream_id)
    if refs:
        singer.resolve_schema_references(schema, refs)
    return schema

def load_metadata(stream, schema):
    mdata = metadata.new()

    mdata = metadata.write(mdata, (), 'table-key-properties', stream.pk_fields)
    mdata = metadata.write(mdata, (), 'forced-replication-method', stream.replication_method)

    if stream.bookmark_key:
        mdata = metadata.write(mdata, (), 'valid-replication-keys', [stream.bookmark_key])

    for field_name in schema['properties'].keys():
        if field_name in stream.pk_fields or field_name == stream.bookmark_key:
            mdata = metadata.write(mdata, ('properties', field_name), 'inclusion', 'automatic')
        else:
            mdata = metadata.write(mdata, ('properties', field_name), 'inclusion', 'available')

    return metadata.to_list(mdata)


def ensure_credentials_are_valid(config):
    XeroClient(config).filter("currencies")

def discover(ctx):
    ctx.check_platform_access()
    catalog = Catalog([])
    for stream in streams_.all_streams:
        schema_dict = load_schema(stream.tap_stream_id)
        mdata = load_metadata(stream, schema_dict)

        schema = Schema.from_dict(schema_dict)
        catalog.streams.append(CatalogEntry(
            stream=stream.tap_stream_id,
            tap_stream_id=stream.tap_stream_id,
            key_properties=stream.pk_fields,
            schema=schema,
            metadata=mdata
        ))
    return catalog


def load_and_write_schema(stream):
    singer.write_schema(
        stream.tap_stream_id,
        load_schema(stream.tap_stream_id),
        stream.pk_fields,
    )


def sync(ctx):
    ctx.refresh_credentials()
    currently_syncing = ctx.state.get("currently_syncing")
    start_idx = streams_.all_stream_ids.index(currently_syncing) \
        if currently_syncing else 0
    stream_ids_to_sync = [cs.tap_stream_id for cs in ctx.catalog.streams
                          if cs.is_selected()]
    streams = [s for s in streams_.all_streams[start_idx:]
               if s.tap_stream_id in stream_ids_to_sync]
    for stream in streams:
        ctx.state["currently_syncing"] = stream.tap_stream_id
        ctx.write_state()
        load_and_write_schema(stream)
        LOGGER.info("Syncing stream: %s", stream.tap_stream_id)
        stream.sync(ctx)
    ctx.state["currently_syncing"] = None
    ctx.write_state()



def main_impl():
    args = utils.parse_args(REQUIRED_CONFIG_KEYS)
    
    if os.getenv("GOOGLE_SECRET_MANAGER", "false").lower() == "true":

        # Access TAP_XERO_REFRESH_TOKEN from Google Secret Manager
        LOGGER.info("Getting TAP_XERO_REFRESH_TOKEN from Google Secret Manager")
        client = secretmanager.SecretManagerServiceClient.from_service_account_file(os.getenv("GOOGLE_APPLICATION_CREDENTIALS"))
        secret_TAP_XERO_REFRESH_TOKEN = f"{os.environ['SECRET_XERO_REFRESH_TOKEN']}/versions/latest"
        secret_XERO_OAUTH_CREDENTIALS = f"{os.environ['SECRET_XERO_OAUTH_CREDENTIALS']}/versions/latest"
        response = client.access_secret_version(request={"name": secret_TAP_XERO_REFRESH_TOKEN})
        args.config['refresh_token'] = response.payload.data.decode("UTF-8")    
        response = client.access_secret_version(request={"name": secret_XERO_OAUTH_CREDENTIALS})
        response_json = json.loads(response.payload.data.decode("UTF-8"))
        args.config['client_id'] = response_json["client_id"]
        args.config['client_secret'] = response_json["client_secret"]
    
    if args.discover:
        discover(Context(args.config, {}, {}, args.config_path)).dump()
        print()
    else:

        if args.catalog:
            catalog = args.catalog
        else:
            LOGGER.info("Running sync without provided Catalog. Discovering.")
            catalog = discover(Context(args.config, {}, {}, args.config_path))

        sync(Context(args.config, args.state, catalog, args.config_path))

    if os.getenv("GOOGLE_SECRET_MANAGER", "false").lower() == "true":
        
        # Write TAP_XERO_REFRESH_TOKEN back to Google Secret Manager
        LOGGER.info("Writing back TAP_XERO_REFRESH_TOKEN to Google Secret Manager")
        secret = f"{os.environ['SECRET_XERO_REFRESH_TOKEN']}"
        version = client.add_secret_version(request={"parent": secret, "payload": {"data": args.config['refresh_token'].encode("UTF-8")}})  

        # Destroy old secret version (to avoid billing)
        newVersionNumber = int(version.name.split('/').pop())
        oldVersionNumber = newVersionNumber - 1
        oldVersion = f"{os.environ['SECRET_XERO_REFRESH_TOKEN']}/versions/{oldVersionNumber}"
        response = client.destroy_secret_version(request={"name": oldVersion})
        LOGGER.info(f"Destroyed version {oldVersionNumber} TAP_XERO_REFRESH_TOKEN from Google Secret Manager")    

def main():
    try:
        main_impl()
    except Exception as exc:
        LOGGER.critical(exc)
        raise exc


if __name__ == "__main__":
    main()
