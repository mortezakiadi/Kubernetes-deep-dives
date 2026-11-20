"""Hand-written explanation for each test scenario. The commands, outputs and check results are NOT written here:
build_writeup.py takes those from the evidence recorded by the test run, so the document cannot drift from what happened.
"""
PROSE = {
    "S01": {
        "intro": "Before testing behaviour I check that the platform is what the design says: an engine on a Multi-AZ database, stateless workers, gateways, a sandbox manager, and no state kept on any disk or "
                 "in any run's name while nobody is working.",
        "trust": [
            "The cluster and the database are described by the AWS APIs, not by my Terraform files. The standby's zone is read from the database itself.",
            "The engine describes itself through its own administration tool, and its pods are counted from the Deployments' status.",
            "'No state on a disk' is the answer of the cluster to a request for every persistent volume claim. 'No sandbox' is the answer for the sandboxes namespace.",
        ],
        "why": [
            "The engine has two copies of each stateful service because the chart is installed with two replicas of each, spread by the scheduler over the zones.",
            "There is no volume because every component I wrote keeps its state in the database or S3. The engine itself keeps its state in PostgreSQL.",
            "There is no sandbox because a sandbox is created only for a step that needs files, and deleted when a run waits or ends.",
        ],
    },
    "S02": {
        "intro": "The design says an agent's state is five different things. I run one agent from a ticket to a finished pull request, stop it where it waits for a reviewer, and look for each of the five kinds in the place "
                 "the design says it lives. The run is set to end its engine history every five steps, so the first kind can be seen too.",
        "trust": [
            "Every number comes from the store it describes: the run's runs and their lengths from the engine, the context from the session database, the snapshots from the database and from S3's own listing, the external effects from the ledger and from the Git host's own counters.",
            "'The run holds nothing while it waits' is the cluster's answer for the sandboxes namespace at the moment the run reports that it is waiting.",
            "The count of model calls is the gateway's table, written by the gateway on every call. The worker cannot affect it.",
        ],
        "why": [
            "The history is short because the workflow ends and restarts itself every five steps, carrying only the session, the step, the last snapshot and the token count.",
            "The context is in the database because every step appends to an append-only log, and a prompt is rebuilt from the log whenever it is needed.",
            "The second snapshot uploaded only one file because snapshots are content-addressed: a file is stored under the hash of its bytes, and the manifest lists hashes.",
            "The branch and the pull request are in the ledger because the tool gateway writes a row before the change and updates it after.",
            "Process memory is the fifth kind and nothing is saved: the workflow and the activities keep nothing between steps that is not in one of the four stores.",
        ],
    },
    "S03": {
        "intro": "The most common failure in the design's story is a worker that disappears in the middle of a step. I kill the worker that is running the test step, with no warning, and watch what the engine does. Then I take away "
                 "every worker, mid-step, and bring two new ones back.",
        "trust": [
            "The worker that is running the step is named by the engine's own record of the pending activity, and I kill that pod by name.",
            "The second attempt is read from the history the engine keeps, not from my logs. The model calls are counted in the gateway's table: a repeated call would be a second row for the same step.",
            "'With no worker, the run still exists' is the engine's answer to a describe request, made while no worker pod exists.",
        ],
        "why": [
            "The engine notices because a running activity must heartbeat. When the heartbeats stop and the timeout passes, the attempt fails and is scheduled again for any worker.",
            "No model call is repeated because the new worker replays the history. Steps with a recorded result return it, and only the step with no result runs.",
            "The run survives with no worker because a run is rows in the engine's database. A worker is only something that polls for tasks.",
        ],
    },
    "S04": {
        "intro": "The model provider fails for minutes at a time. I make the primary provider unavailable and run three situations: a run that may use a fallback, a run that may not and must wait, and five runs paused by an operator "
                 "for the length of the incident and then released at a limited rate.",
        "trust": [
            "Every call the gateway receives is a row in its table with the backend that served it and the status it returned. The checks read that table.",
            "The provider's failure is switched on and off in the gateway by a command, so the start and end of the incident are known exactly.",
            "'No call while paused' compares the number of rows in the table for the five runs before and after a 25-second wait.",
        ],
        "why": [
            "The first run never saw an error because the gateway sends the call to the fallback when the primary does not answer.",
            "The second run was slow and then fine because a refused call is a failed activity, and the engine retries it with backoff (2, 4, 8 seconds and on) until the provider answers. The attempts and the last failure are in the history.",
            "Paused runs make no calls because a pause is a signal that the workflow honours at the top of its loop, where it waits. Nothing retries against a provider that is down.",
            "The resume took five seconds because the operator command releases one run a second. A thousand runs released at once would hit the provider together.",
        ],
    },
    "S05": {
        "intro": "A retry cannot tell 'it failed' from 'it worked but the answer was lost'. I make the Git host lose its answer after it has made a change, which is the cause of the duplicate pull requests in the design's story. "
                 "First an unprotected tool as a control, then the protected tools with every kind of change losing its answer once, then a tool that cannot be protected.",
        "trust": [
            "The Git host is the external system and it counts what it holds. A duplicate shows as a count, not as a log line.",
            "The lost answer is not simulated in the platform: the host performs the change, commits it, and then answers with an error, so the tool gateway sees exactly what it would see with a real timeout.",
            "The control is in the result on purpose. The same fault against an unprotected tool opens two pull requests, so the protected result is not an artefact of a fault that never happened.",
            "The ledger and the host agree: the ledger's attempt counts and deduplication counts are read from the database, and the host's counts are read from the host.",
        ],
        "why": [
            "The branch and the pull request were not duplicated because their tools are 'checkable': on a second attempt the gateway first asks the host whether the effect is already there, and uses it if so.",
            "The comment was not duplicated because it is 'idempotent': the second attempt carries the same key as the first, and the host returns the first comment.",
            "The key is the same because it is built from the session and the step, never from the attempt.",
            "The run that deletes a branch stopped because that tool is 'unsafe': it cannot be keyed or checked, so a second attempt after an unknown outcome is refused and the workflow moves the run to needs attention.",
        ],
    },
    "S06": {
        "intro": "A sandbox can disappear at any time, with its disk. I delete a run's sandbox while its tests are running and see whether the files come back from the last snapshot. Then I leave a run waiting for a reviewer, remove every worker, "
                 "approve with nothing running, and bring workers back.",
        "trust": [
            "The sandbox is deleted by name with no grace period. The snapshots that exist at that moment are listed from the database first.",
            "'The files came back' rests on the output of the test run that followed, which says it ran two tests and passed. That output is the result the agent itself saw, read from the context log. A new empty sandbox cannot produce it, because the test files are not on the image.",
            "'Nothing is held while waiting' is the cluster's listing of the sandboxes namespace and of the worker pods. 'The approval is safe' is the engine's answer after 20 seconds with no worker.",
        ],
        "why": [
            "The test step failed and was retried because the connection to its sandbox broke. The engine records the attempt count.",
            "The new sandbox had the files because the sandbox manager restores the run's last snapshot into every sandbox it creates: it downloads the manifest and the files it names.",
            "The model was not called again because a sandbox loss is a tool failure. It does not change the workflow's decisions.",
            "The approval was kept because a signal is an event in the run's history. When workers returned, one replayed the history and found the approval.",
        ],
    },
    "S07": {
        "intro": "The model sees what the platform builds for it, and a restarted worker remembers nothing. I kill a worker in the middle of a slow model call and compare the prompt that the next worker sends. Then I run the same task "
                 "with and without compaction, and rebuild every prompt of both runs from the database to compare each one's hash with the hash recorded when it was sent.",
        "trust": [
            "Every call is a row in the gateway's table with a SHA-256 of the prompt it received. Equal hashes mean equal prompts.",
            "The rebuild runs inside the cluster against the database as it is at that moment. If a prompt depended on anything else (a worker's memory, the order of rows, the time), the hashes would differ.",
            "The prompt sizes come from the same table.",
        ],
        "why": [
            "The second attempt sent the same prompt because the prompt is a function of the log and the latest summary, and both are in the database. The worker that started the call had nothing the next one lacked.",
            "The model was asked twice for that step, and that is the one cost: the call was in flight when its worker died, and the platform cannot know that the provider did not finish it.",
            "Compaction shrank the prompt because a summary, which is stored and recorded like any other step, replaced the entries before it.",
            "The compacted run reached the same end because the summary carries what the next decision needs: the task, the tools done and the facts learned.",
        ],
    },
    "S08": {
        "intro": "People steer runs. I do to runs what an operator or a reviewer would: pause and resume one, hit a step limit and a token limit, give one a model that repeats itself, cancel one that has a pull request, "
                 "and start new runs from the middle of an old one.",
        "trust": [
            "The state of each run is the workflow's own answer to a query, plus the engine's record. The step counts and the model-call counts are read from the engine and the gateway.",
            "The effects of cancelling are read from the Git host: a closed pull request and a deleted branch are counts and states in the host, and the ledger's statuses are read from the database.",
            "The fork's hint is checked in the fork's own context log, where the model's next write after the review contains it.",
        ],
        "why": [
            "A pause stopped the run after its step and not in the middle because the workflow checks the pause flag at the top of its loop.",
            "The limits stop a run because the workflow checks the step budget before each model call, and the gateway refuses a call that would take the run over its token budget.",
            "The loop was found because the workflow keeps the last three calls and compares them. The third identical call is not run.",
            "Cancel undid the pull request and the branch because the workflow's cancel path asks the tool gateway to compensate, newest first, from the ledger.",
            "A fork or a rewind starts a new workflow from the context up to step N and the workspace snapshot of that step. A rewind also cancels the original, which undoes the original's effects.",
        ],
    },
    "S09": {
        "intro": "Deploying new agent code is dangerous for a different reason than deploying a service: every run that is waiting will be replayed through the new code. I replay a waiting run through three versions of the workflow, "
                 "deploy the safe one beside the old one, and then stop a worker politely.",
        "trust": [
            "A replay is the engine's own replayer run against the recorded history. The result is the replayer's, and nothing outside the process is touched.",
            "The queue each run lives on is read from the engine. The lint steps are counted in each run's history.",
            "'Nothing is left on version 1' is the engine's list of running runs, filtered by queue.",
        ],
        "why": [
            "Version 2 without protection fails to replay because the recorded history says 'snapshot, then run the next tool' and the new code says 'snapshot, then lint'. The engine reports a nondeterminism error.",
            "Version 2 with a patch marker replays because the marker is false for a history recorded before it existed.",
            "The old run stayed on version 1 because a run keeps the queue it started on. The new run was on version 2 because the session API's current queue had moved.",
            "The polite stop left the step alone because the worker stops taking tasks on SIGTERM and lets activities in flight finish, within a 50-second grace.",
        ],
    },
    "S10": {
        "intro": "The engine is the most important dependency. I force the Multi-AZ database to fail over to its standby while three runs are in flight, and then delete every engine pod while another run is in flight. Nobody touches the runs.",
        "trust": [
            "The failover is the one the AWS API reports: the database's primary zone before and after, and its status.",
            "The runs' end states and the Git host's counts are read after the fact. The repeated model calls are counted from the gateway's table, and I print the count whether it is zero or not.",
            "The pods that replaced the engine's are listed before and after by name. No name is in both lists.",
        ],
        "why": [
            "The runs continued because everything that talks to the database retries its connection, and the new primary answers on the same name.",
            "No pull request was duplicated because the ledger lives in the same database, and a step that was in flight when the database moved is retried with the same key.",
            "The engine's pods were replaceable because the engine keeps no state of its own: it reads each run's history from PostgreSQL.",
        ],
    },
    "S11": {
        "intro": "At scale the questions are different: do the workers grow when the work grows, does the work still happen once, and can an operator see what is going on. I start 24 runs at once on workers that can each do six things at once, "
                 "so two workers cannot hold them, and read the cluster, the engine and the gateway while it happens.",
        "trust": [
            "The worker count is the cluster's list of worker pods, sampled every ten seconds. The slots in use are Prometheus's reading of the workers' own metric.",
            "The effects (24 branches, 24 pull requests) are the Git host's counts. The tokens are the gateway's table.",
            "The wait for a free slot is the engine's own histogram, read from Prometheus. I print it as it is.",
        ],
        "why": [
            "The workers saturated because 24 runs each had a long test step and there were 12 slots. The rest of the steps queued in the engine.",
            "The workers grew because KEDA reads each task queue's backlog from the engine and sets the Deployment's size so that no worker has more than a few waiting tasks.",
            "Nothing was repeated because scaling out adds pollers. A task goes to one of them, and a task whose worker dies is retried under the same key.",
            "Cost per run is a query because every model call goes through the gateway, which writes one row for each.",
        ],
    },
}
