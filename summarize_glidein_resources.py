#!/usr/bin/env python3

"""
Aggregate machine ads into time bins by site
"""

from argparse import ArgumentParser, ArgumentDefaultsHelpFormatter
import elasticsearch
import elasticsearch_dsl as edsl
import datetime
import dateutil
import re
import logging
import time
from urllib.parse import urlparse, urlunparse


def parse_timedelta(time_str):
    parts = re.match(
        r"((?P<days>(\d+?\.?\d*))d)?((?P<hours>(\d+?\.?\d*))h)?((?P<minutes>(\d+?\.?\d*))m)?((?P<seconds>(\d+?\.?\d*))s)?",
        time_str,
    )
    if not parts:
        raise ValueError
    parts = parts.groupdict()
    if not any([v is not None for v in list(parts.values())]):
        raise ValueError
    time_params = {}
    for (name, param) in list(parts.items()):
        if param:
            time_params[name] = float(param)
    return datetime.timedelta(**time_params)


def get_datetime(value):
    try:
        return datetime.datetime.utcnow() - parse_timedelta(value)
    except ValueError:
        return dateutil.parser.parse(value)


def snap_to_interval(dt, interval):
    ts = time.mktime(dt.timetuple())
    ts = ts - (ts % int(interval.total_seconds()))
    return datetime.datetime.utcfromtimestamp(ts)


def parse_index(url_str):
    url = urlparse(url_str)
    return {
        "host": urlunparse(url._replace(path="", params="", query="", fragment="")),
        "index": url.path[1:],
    }


parser = ArgumentParser(
    description=__doc__, formatter_class=ArgumentDefaultsHelpFormatter
)
parser.add_argument(
    "--after", default="2d", help="maximum time to look back", type=get_datetime,
)
parser.add_argument(
    "--before", default="0d", help="minimum time to look back", type=get_datetime,
)
parser.add_argument(
    "--interval", default="20m", help="aggregation interval", type=parse_timedelta,
)
parser.add_argument(
    "-y",
    "--dry-run",
    default=False,
    action="store_true",
    help="query status, but do not ingest into ES",
)
parser.add_argument(
    "-v",
    "--verbose",
    default=False,
    action="store_true",
    help="use verbose logging in ES",
)
parser.add_argument(
    "-i",
    "--input-index",
    type=parse_index,
    default="http://elk-1.icecube.wisc.edu:9200/condor_status",
)
parser.add_argument(
    "-o",
    "--output-index",
    type=parse_index,
    default="http://elk-1.icecube.wisc.edu:9200/glidein_resources",
)

options = parser.parse_args()

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s : %(message)s"
)
if options.verbose:
    logging.getLogger("elasticsearch").setLevel("DEBUG")

# round time range to nearest interval
after = snap_to_interval(options.after, options.interval)
# ...only if last bin is far enough in the past to be complete
if datetime.datetime.utcnow() - options.before > options.interval:
    before = snap_to_interval(options.before, options.interval)
else:
    before = options.before

if not before > after:
    parser.error("--before must be > --after")

# note different capitalization conventions for GPU and Cpu
RESOURCES = ("GPUs", "Cpus", "Memory", "Disk")
STATUSES = ("evicted", "removed", "finished", "failed")

# Accumulate offered and claimed resources in time bins, weighting by the
# fraction of each bin that intersects the glidein lifetime
summarize_resources = edsl.A(
    "scripted_metric",
    init_script="""
        state.interval = (Long)(params.interval);
        HashMap metrics = new HashMap();
        for (resource in params.RESOURCES) {
            for (status in params.STATUSES) {
                String key = "claimed."+status+"."+resource;
                metrics.put(key, 0.0);
            }
            metrics.put("offered."+resource, 0.0);
        }
        state.metrics = metrics;
        """,
    map_script="""
        // The time range of each item intersects one or more buckets, but does not
        // necessarily overlap each completely. Ideally we would use the exact overlap
        // fraction to weight contributions to each bucket, but since Elastic does not
        // give us access to the bucket key, we have to settle for the average overlap
        // fraction.
        long left = doc[params.left].value.toInstant().toEpochMilli();
        long right = doc[params.right].value.toInstant().toEpochMilli();
        long total_interval = (state.interval*((right+params.interval)/state.interval-left/state.interval));
        double active_fraction = (right-left).doubleValue()/total_interval.doubleValue();
        HashMap metrics = state.metrics;
        for (resource in params.RESOURCES) {
            if (!doc.containsKey("Total"+resource)) {
                continue;
            }
            double capacity = doc["Total"+resource].value.doubleValue();
            for (status in params.STATUSES) {
                String source = "occupancy."+status+"."+resource;
                String dest = "claimed."+status+"."+resource;
                if (doc.containsKey(source)) {
                    metrics[dest] += active_fraction*doc[source].value*capacity;
                }
            }
            metrics["offered."+resource] += active_fraction*capacity;
        }
        """,
    combine_script="""
        return state.metrics;
        """,
    reduce_script="""
        Map aggregate = new HashMap();
        for (state in states) {
            if (state == null) {
                continue;
            }
            for (entry in state.entrySet()) {
                if (aggregate.containsKey(entry.getKey())) {
                    aggregate[entry.getKey()] += entry.getValue();
                } else {
                    aggregate[entry.getKey()] = entry.getValue();
                }
            }
        }
        return aggregate;
        """,
    params={
        "left": "DaemonStartTime",
        "right": "LastHeardFrom",
        "interval": int(options.interval.total_seconds() * 1000),
        "RESOURCES": RESOURCES,
        "STATUSES": STATUSES + ("total",),
    },
)


def scan_aggs(search, source_aggs, inner_aggs={}, size=10):
    """
    Helper function used to iterate over all possible bucket combinations of
    ``source_aggs``, returning results of ``inner_aggs`` for each. Uses the
    ``composite`` aggregation under the hood to perform this.
    """

    def run_search(**kwargs):
        s = search[:0]
        s.aggs.bucket("comp", "composite", sources=source_aggs, size=size, **kwargs)
        for agg_name, agg in list(inner_aggs.items()):
            s.aggs["comp"][agg_name] = agg
        return s.execute()

    response = run_search()
    while response.aggregations.comp.buckets:
        for b in response.aggregations.comp.buckets:
            yield b
        if "after_key" in response.aggregations.comp:
            after = response.aggregations.comp.after_key
        else:
            after = response.aggregations.comp.buckets[-1].key
        response = run_search(after=after)


def resource_summaries(host, index, after, before, interval):
    by_site = [
        {k: edsl.A("terms", field=k + ".keyword")}
        for k in ("site", "country", "institution", "resource")
    ]
    # split sites into GPU/CPU partitions
    by_site.append(
        {"slot_type": edsl.A("terms", script='doc.TotalGPUs.value > 0 ? "GPU" : "CPU"')}
    )
    # NB: @timestamp is not included in the composite aggregation, as this
    # buckets documents for _every_ combination of the source values, meaning
    # that a document will be added to the bucket N times if N of its
    # @timestamp values fall into the time range. To emulate ES 7.x range
    # semantics (one doc falls in many buckets, each bucket sees only one copy
    # of each doc), we split date_histogram off into a sub-aggregation.
    by_timestamp = edsl.A(
        "date_histogram",
        field="@timestamp",
        interval=int(interval.total_seconds() * 1000),
    )
    by_timestamp.bucket("resources", summarize_resources)

    buckets = scan_aggs(
        (
            edsl.Search()
            .using(elasticsearch.Elasticsearch(host))
            .index(index)
            .filter("range", **{"@timestamp": {"gte": after, "lt": before}})
        ),
        by_site,
        {"timestamp": by_timestamp},
        size=1,
    )
    for site in buckets:
        for bucket in site.timestamp.buckets:
            # Filter buckets to query time range. This should be possible to do
            # in the query DSL, but bucket_selector does not support
            # date_histogram buckets, and the corresponding ticket has been
            # open for years:
            # https://github.com/elastic/elasticsearch/issues/23874
            timestamp = datetime.datetime.utcfromtimestamp(bucket.key / 1000)
            if timestamp >= after and timestamp < before and bucket.doc_count > 0:
                data = bucket.resources.value.to_dict()
                data["count"] = bucket.doc_count
                data["_keys"] = site.key.to_dict()
                data["_keys"]["timestamp"] = timestamp.strftime("%Y-%m-%dT%H:%M:%S")
                yield data


buckets = resource_summaries(
    options.input_index["host"],
    options.input_index["index"],
    after,
    before,
    options.interval,
)


def make_insert(
    generator,
    index=options.output_index["index"],
    id_keys=["timestamp", "resource", "site", "slot_type"],
):
    for entry in generator:
        data = dict(entry)
        data["_index"] = index
        data["_type"] = "resource_summary"
        key = data.pop("_keys")
        data["_id"] = ".".join([key[k] for k in id_keys])
        data.update(key)
        yield data


if options.dry_run:
    import json
    import sys

    for bucket in make_insert(buckets):
        json.dump(bucket, sys.stdout)
        sys.stdout.write("\n")
else:
    es = elasticsearch.Elasticsearch(hosts=options.output_index["host"], timeout=5000)
    index = options.output_index["index"]

    success, _ = elasticsearch.helpers.bulk(
        es, make_insert(buckets), max_retries=20, initial_backoff=2, max_backoff=3600,
    )
