import json
import os
import time

import requests
import yaml
from loguru import logger
from huggingface_hub import HfApi

from demo import LoraTrainingArguments, train_lora
from utils.constants import model2base_model, model2size
from utils.flock_api import get_task, submit_task

import sys
from git import Repo, GitCommandError

HF_USERNAME = os.environ["HF_USERNAME"]


def check_and_update_repo():
    repo_path = os.path.dirname(os.path.abspath(__file__))
    repo = Repo(repo_path)

    try:
        # Get the remote repository reference
        origin = repo.remotes.origin
        origin.fetch()

        # Get the latest local and remote commits
        local_commit = repo.head.commit
        logger.info(f"Local commit: {local_commit.hexsha}")
        
        remote_commit = repo.refs['origin/main'].commit
        logger.info(f"Remote commit: {remote_commit.hexsha}")

        # Check if the local repository is up to date with the remote repository
        if local_commit != remote_commit:
            # Display a prominent warning
            logger.warning("Your repository is not up to date.")
            logger.warning("Warning: A new version is available!")
            logger.warning("Please use the following command to update the code:")
            logger.warning("git pull")


        else:
            logger.info("Your repository is up to date.")

    except GitCommandError as e:
        logger.error(f"Error checking or updating repository: {e}")

if __name__ == "__main__":
    # Call the update check function during library initialization
    check_and_update_repo()
    task_id = os.environ["TASK_ID"]
    # load trainin args
    # define the path of the current file
    current_folder = os.path.dirname(os.path.realpath(__file__))
    with open(f"{current_folder}/training_args.yaml", "r") as f:
        all_training_args = yaml.safe_load(f)

    task = get_task(task_id)
    # log the task info
    logger.info(json.dumps(task, indent=4))
    # download data from a presigned url
    data_url = task["data"]["training_set_url"]
    context_length = task["data"]["context_length"]
    max_params = task["data"]["max_params"]

    # filter out the model within the max_params
    model2size = {k: v for k, v in model2size.items() if v <= max_params}
    all_training_args = {k: v for k, v in all_training_args.items() if k in model2size}
    logger.info(f"Models within the max_params: {all_training_args.keys()}")
    # download in chunks
    response = requests.get(data_url, stream=True)
    with open("demo_data.jsonl", "wb") as f:
        for chunk in response.iter_content(chunk_size=8192):
            f.write(chunk)

    # train all feasible models and merge
    for model_id in all_training_args.keys():
        logger.info(f"Start to train the model {model_id}...")
        # if OOM, proceed to the next model
        try:
            train_lora(
                model_id=model_id,
                context_length=context_length,
                training_args=LoraTrainingArguments(**all_training_args[model_id]),
            )
        except RuntimeError as e:
            logger.error(f"Error: {e}")
            logger.info("Proceed to the next model...")
            continue

        # generate a random repo id based on timestamp
        hg_repo_id = f"{model_id.replace('/', '-')}-" + str(int(time.time()))

        try:
            logger.info("Start to push the lora weight to the hub...")
            api = HfApi(token=os.environ["HF_TOKEN"])
            api.create_repo(
                f"{HF_USERNAME}/{hg_repo_id}",
                repo_type="model",
            )
            api.upload_folder(
                folder_path="outputs",
                repo_id=f"{HF_USERNAME}/{hg_repo_id}",
                repo_type="model",
            )
            # submit
            submit_task(
                task_id, f"{HF_USERNAME}/{hg_repo_id}", model2base_model[model_id]
            )
            logger.info("Task submitted successfully")
        except Exception as e:
            logger.error(f"Error: {e}")
            logger.info("Proceed to the next model...")
        finally:
            # cleanup merged_model and output
            os.system("rm -rf merged_model")
            os.system("rm -rf outputs")
            continue
