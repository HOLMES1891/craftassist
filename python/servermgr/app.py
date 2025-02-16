"""
Copyright (c) Facebook, Inc. and its affiliates.
"""

import gzip
import json
import logging
import os
import random
import socket
import time
from base64 import b64encode, b64decode

import boto3
import botocore
import flask
import redis

app = flask.Flask(__name__)
ec2 = boto3.resource("ec2")
ecs = boto3.client("ecs")
s3 = boto3.resource("s3")

import ping_cuberite

logging.basicConfig(format="%(asctime)s [%(levelname)s]: %(message)s")
logging.getLogger().setLevel(logging.INFO)

AWS_ACCESS_KEY_ID = os.environ["AWS_ACCESS_KEY_ID"]
AWS_SECRET_ACCESS_KEY = os.environ["AWS_SECRET_ACCESS_KEY"]

SUBNET_IDS = ["subnet-bee9d9d9"]
SECURITY_GROUP_IDS = ["sg-04ec8fa6e1d91d460"]

with open("run.withagent.sh", "rb") as f:
    txt = f.read()
    txt_flat = txt.replace(b"diverse_world", b"flat_world")
    run_sh_gz_b64 = b64encode(gzip.compress(txt)).decode("utf-8")
    run_flat_sh_gz_b64 = b64encode(gzip.compress(txt_flat)).decode("utf-8")

logging.info("Connecting to redis at {}".format(os.environ["REDIS_URL"]))
rconn = redis.from_url(os.environ["REDIS_URL"])


@app.route("/")
@app.route("/emnlp2019")
def homepage():
    logging.info("cookie instance_id={}".format(flask.request.cookies.get("instance_id", "")))
    return flask.render_template("index.html")


@app.route("/launch", methods=["GET", "POST"])
def launch():
    logging.info("Launching instance")
    instance_id = launch_instance()
    logging.info("Launched instance: {}".format(instance_id))
    response = app.make_response(flask.redirect("/wait/{}".format(urlencode(instance_id))))
    response.set_cookie("instance_id", instance_id)
    return response


@app.route("/launch/<config>", methods=["GET", "POST"])
def launch_config(config):
    logging.info("Launching instance")
    instance_id = launch_instance(config=config)
    logging.info("Launched instance: {}".format(instance_id))
    response = app.make_response(flask.redirect("/wait/{}".format(urlencode(instance_id))))
    response.set_cookie("instance_id", instance_id)
    return response


@app.route("/wait/<enc_instance_id>")
def wait(enc_instance_id):
    instance_id = urldecode(enc_instance_id)
    logging.info("Waiting for instance {}".format(instance_id))
    return flask.render_template(
        "wait.html",
        instance_id=instance_id,
        role=flask.request.args.get("role"),
        hide_survey=bool(flask.request.args.get("role")),
    )


@app.route("/status")
def status():
    instance_id = flask.request.args["q"]

    if instance_id == "test":
        return json.dumps({"progress": 100, "ip": "123.123.123.123"})

    logging.info("status: fetching instance")
    x = ecs.describe_tasks(cluster="craftassist", tasks=[instance_id])
    try:
        attachment_id = x["tasks"][0]["containers"][0]["networkInterfaces"][0]["attachmentId"]
        attachment = next(y for y in x["tasks"][0]["attachments"] if y["id"] == attachment_id)
    except:
        return json.dumps({"progress": 30})

    try:
        eni = next(y for y in attachment["details"] if y["name"] == "networkInterfaceId")["value"]
        ip = ec2.NetworkInterface(eni).private_ip_addresses[0]["Association"]["PublicIp"]
    except:
        return json.dumps({"progress": 50, "ip": None})

    try:
        logging.info("status: trying socket connect")
        s = socket.socket()
        s.settimeout(10)
        s.connect((ip, 25565))
        s.close()
    except:
        return json.dumps({"progress": 75, "ip": ip})
    try:
        logging.info("status: trying ping")
        ping_cuberite.ping(ip, 25565, timeout=1)
    except:
        return json.dumps({"progress": 90, "ip": ip})

    logging.info("status: success")
    return json.dumps({"progress": 100, "ip": ip})


@app.route("/matchmaker")
def matchmaker_home():
    return flask.render_template("matchmaker.html")


@app.route("/matchmaker/launch", methods=["GET", "POST"])
def matchmaker_launch():
    SCRIPT = """
local prefix = 'matchmaker:'
local wait_key = redis.call('GET', prefix..'wait_key')
if not wait_key then
    local wait_key = prefix..math.random()
    redis.call('SET', prefix..'wait_key', wait_key, 'EX', 600)
    return cjson.encode({action="launch", wait_key=wait_key})
else
    redis.call('DEL', prefix..'wait_key')
    return cjson.encode({action="wait", wait_key=wait_key})
end
    """
    r = rconn.eval(SCRIPT, numkeys=0)
    d = json.loads(r)
    logging.info("matchmaker script returned {}".format(d))

    if d["action"] == "launch":
        instance_id = launch_instance("craftassist-server")
        rconn.set(d["wait_key"], instance_id, ex=3600)
        return flask.redirect("/wait/{}?role=manager".format(urlencode(instance_id)))

    elif d["action"] == "wait":
        while True:
            instance_id = rconn.get(d["wait_key"])
            if instance_id:
                return flask.redirect("/wait/{}?role=assistant".format(urlencode(instance_id)))
            else:
                time.sleep(0.5)


@app.route("/survey", methods=["POST"])
def survey():
    instance_id = flask.request.form["instance_id"]
    form_data = json.dumps(flask.request.form)
    s3.Bucket("craftassist").put_object(Key="surveys/{}".format(instance_id), Body=form_data)
    return flask.redirect("/")


@app.route("/clear")
def clear():
    response = app.make_response(flask.redirect("/"))
    response.set_cookie("instance_id", "", expires=0)
    return response


def launch_instance(task="craftassist", config="random", debug=False):
    """Returns instance id (specifically, ECS task ARN) of a newly launched instance.

    Instance is not yet ready, and may not even have an ip address assigned!
    """
    if config == "diverse_world":
        run_sh = run_sh_gz_b64
    elif config == "flat_world":
        run_sh = run_flat_sh_gz_b64
    elif config == "random":
        run_sh = random.choice([run_sh_gz_b64, run_flat_sh_gz_b64])
    else:
        raise ValueError("Bad config={}".format(config))

    r = ecs.run_task(
        cluster="craftassist",
        taskDefinition=task,
        count=1,
        launchType="FARGATE",
        networkConfiguration={
            "awsvpcConfiguration": {
                "subnets": SUBNET_IDS,
                "securityGroups": SECURITY_GROUP_IDS,
                "assignPublicIp": "ENABLED",
            }
        },
        overrides={
            "containerOverrides": [
                {
                    "name": "craftassist",
                    "environment": [
                        {"name": "RUN_SH_GZ_B64", "value": run_sh},
                        {"name": "AWS_ACCESS_KEY_ID", "value": AWS_ACCESS_KEY_ID},
                        {"name": "AWS_SECRET_ACCESS_KEY", "value": AWS_SECRET_ACCESS_KEY},
                        {
                            "name": "SENTRY_DSN",
                            "value": os.environ.get("CRAFTASSIST_SENTRY_DSN", ""),
                        },
                    ],
                }
            ]
        },
    )
    logging.info("Launched: {}".format(r))
    return r["tasks"][0]["taskArn"]


def is_expired(instance_id):
    try:
        task = ecs.describe_tasks(cluster="craftassist", tasks=[instance_id])["tasks"][0]
    except botocore.exceptions.ClientError as e:
        if e.response.get("Error", {}).get("Code") != "InvalidInstanceID.NotFound":
            raise
        logging.info("Instance {} does not exist".format(instance_id))
        return True
    except IndexError as e:
        logging.info("Instance {} does not exist".format(instance_id))
        return True

    state = task["lastStatus"]
    if state not in ("RUNNING", "PROVISIONING", "PENDING"):
        logging.info("Instance {} is {}".format(instance_id, state))
        return True
    return False


def urlencode(s):
    """str -> b64 str"""
    if type(s) == str:
        s = s.encode()
    return b64encode(s).decode()


def urldecode(s):
    """b64 str -> str"""
    return b64decode(s).decode()


if __name__ == "__main__":
    logging.getLogger(__name__).setLevel(logging.INFO)
    app.run(debug=True, use_reloader=True)
else:
    gunicorn_logger = logging.getLogger("gunicorn.error")
    logging.getLogger(__name__).handlers.extend(gunicorn_logger.handlers)
    logging.getLogger(__name__).setLevel(gunicorn_logger.level)
