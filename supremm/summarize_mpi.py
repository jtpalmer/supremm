#!/usr/bin/env python
"""
    Main script for converting host-based pcp archives to job-level summaries.
"""

from mpi4py import MPI

import logging
from supremm.config import Config
from supremm.account import DbAcct
from supremm.xdmodaccount import XDMoDAcct
from supremm.pcparchive import extract_and_merge_logs
from supremm import outputter
from supremm.summarize import Summarize
from supremm.plugin import loadplugins, loadpreprocessors
from supremm.scripthelpers import parsetime
from supremm.scripthelpers import setuplogger
from supremm.errors import ProcessingError

import sys
import os
from getopt import getopt
import traceback
import time
import datetime
import shutil
import psutil
import json

def usage():
    """ print usage """
    print "usage: {0} [OPTS]".format(os.path.basename(__file__))
    print "  -j --localjobid JOBID process only the job with the provided local job id"
    print "                        (resource must also be specified)"
    print "  -r --resource RES     process only jobs on the specified resource"
    print "  -d --debug            set log level to debug"
    print "  -q --quiet            only log errors"
    print "  -s --start TIME       process all jobs that ended after the provided start"
    print "                        time (an end time must also be specified)"
    print "  -e --end TIME         process all jobs that ended before the provided end"
    print "                        time (a start time must also be specified)"
    print "  -A --process-all      when using a timerange, look for all jobs. Combines flags (BONC)"
    print "  -B --process-bad      when using a timerange, look for jobs that previously failed to process"
    print "  -O --process-old      when using a timerange, look for jobs that have an old process version"
    print "  -N --process-notdone  when using a timerange, look for unprocessed jobs"
    print "  -C --process-current  when using a timerange, look for jobs with the current process version"
    print "  -b --process-big      when using a timerange, look for jobs that were previously marked as being too big"
    print "  -P --process-error N  when using a timerange, look for jobs that were previously marked with error N"
    print "  -T --timeout SECONDS  amount of elapsed time from a job ending to when it"
    print "  -M --max-nodes NODES  only process jobs with fewer than this many nodes"
    print "                        can be marked as processed even if the raw data is"
    print "                        absent"
    print "  -t --tag              tag to add to the summarization field in mongo"
    print "  -D --delete T|F       whether to delete job-level archives after processing."
    print "  -E --extract-only     only extract the job-level archives (sets delete=False)"
    print "  -L --use-lib-extract  use libpcp_pmlogextract.so.1 instead of pmlogextract"
    print "  -o --output DIR       override the output directory for the job archives."
    print "                        This directory will be emptied before used and no"
    print "                        subdirectories will be created. This option is ignored "
    print "                        if multiple jobs are to be processed."
    print "  -h --help             display this help message and exit."


def getoptions():
    """ process comandline options """

    localjobid = None
    resource = None
    starttime = None
    endtime = None
    joboutdir = None

    retdata = {
        "log": logging.INFO,
        "dodelete": True,
        "extractonly": False,
        "libextract": False,
        "process_all": False,
        "process_bad": False,
        "process_old": False,
        "process_notdone": False,
        "process_current": False,
        "process_big": False,
        "process_error": 0,
        "max_nodes": 0,
        "job_output_dir": None,
        "tag": None,
        "force_timeout": 2 * 24 * 3600,
        "resource": None
    }

    opts, _ = getopt(sys.argv[1:], "ABONCbM:j:r:dqs:e:LT:t:D:Eo:h", 
                     ["localjobid=", 
                      "resource=", 
                      "debug", 
                      "quiet", 
                      "start=", 
                      "end=", 
                      "process-all",
                      "process-bad",
                      "process-old",
                      "process-notdone",
                      "process-current",
                      "process-big",
                      "max-nodes=",
                      "timeout=", 
                      "tag=",
                      "delete=", 
                      "extract-only", 
                      "use-lib-extract", 
                      "output=", 
                      "help"])

    for opt in opts:
        if opt[0] in ("-j", "--jobid"):
            localjobid = opt[1]
        if opt[0] in ("-r", "--resource"):
            resource = opt[1]
        if opt[0] in ("-d", "--debug"):
            retdata['log'] = logging.DEBUG
        if opt[0] in ("-q", "--quiet"):
            retdata['log'] = logging.ERROR
        if opt[0] in ("-s", "--start"):
            starttime = parsetime(opt[1])
        if opt[0] in ("-e", "--end"):
            endtime = parsetime(opt[1])
        if opt[0] in ("-A", "--process-all"):
            retdata['process_all'] = True
        if opt[0] in ("-B", "--process-bad"):
            retdata['process_bad'] = True
        if opt[0] in ("-O", "--process-old"):
            retdata['process_old'] = True
        if opt[0] in ("-N", "--process-notdone"):
            retdata['process_notdone'] = True
        if opt[0] in ("-C", "--process-current"):
            retdata['process_current'] = True
        if opt[0] in ("-b", "--process-big"):
            retdata['process_big'] = True
        if opt[0] in ("-P", "--process-error"):
            retdata['process_error'] = int(opt[1])
        if opt[0] in ("-L", "--use-lib-extract"):
            retdata['libextract'] = True
        if opt[0] in ("-M", "--max-nodes"):
            retdata['max_nodes'] = int(opt[1])
        if opt[0] in ("-T", "--timeout"):
            retdata['force_timeout'] = int(opt[1])
        if opt[0] in ("-t", "--tag"):
            retdata['tag'] = str(opt[1])
        if opt[0] in ("-D", "--delete"):
            retdata['dodelete'] = True if opt[1].upper().startswith("T") else False
        if opt[0] in ("-E", "--extract-only"):
            retdata['extractonly'] = True
        if opt[0] in ("-o", "--output"):
            joboutdir = opt[1]
        if opt[0] in ("-h", "--help"):
            usage()
            sys.exit(0)

    if retdata['extractonly']:
        # extract-only supresses archive delete
        retdata['dodelete'] = False

    # If all options selected, treat as all to optimize the job selection query
    if retdata['process_bad'] and retdata['process_old'] and retdata['process_notdone'] and retdata['process_current']:
        retdata['process_all'] = True

    if not (starttime == None and endtime == None):
        if starttime == None or endtime == None:
            usage()
            sys.exit(1)
        retdata.update({"mode": "timerange", "start": starttime, "end": endtime, "resource": resource})
        # Preserve the existing mode where just specifying a timerange does all jobs
        if not retdata['process_bad'] and not retdata['process_old'] and not retdata['process_notdone'] and not retdata['process_current'] and not retdata['process_big'] and retdata['process_error']==0:
            retdata['process_all'] = True
        return retdata
    else:
        if not retdata['process_bad'] and not retdata['process_old'] and not retdata['process_notdone'] and not retdata['process_current'] and not retdata['process_big'] and retdata['process_error']==0:
            # Preserve the existing mode where unprocessed jobs are selected when no time range given
            retdata['process_bad'] = True
            retdata['process_old'] = True
            retdata['process_notdone'] = True
        if (retdata['process_bad'] and retdata['process_old'] and retdata['process_notdone'] and retdata['process_current']) or retdata['process_all']:
            # Sanity checking to not do every job in the DB
            logging.error("Cannot process all jobs without a time range")
            sys.exit(1)

    if localjobid == None and resource == None:
        retdata.update({"mode": "all"})
        return retdata

    if localjobid != None and resource != None:
        retdata.update({"mode": "single", "local_job_id": localjobid, "resource": resource, "job_output_dir": joboutdir})
        return retdata

    if resource != None:
        retdata.update({"mode": "resource", "resource": resource})
        return retdata

    usage()
    sys.exit(1)


def summarizejob(job, conf, resconf, plugins, preprocs, m, dblog, opts):
    """ Main job processing, Called for every job to be processed """

    success = False

    try:

        # Log and don't process the jobs that match these WHERE clauses:
        #                 AND NOT (jf.nodecount > 1 AND jf.wallduration < 300)
        #                 AND jf.nodecount < 10000
        #                 AND jf.wallduration < 176400
        #                 AND jf.wallduration > 180

            
        mdata = {}
        mergestart = time.time()

        summarizeerror=None

        if job.nodecount > 1 and job.walltime < 5 * 60:
            mergeresult = 1
            mdata["skipped_parallel_too_short"] = True
            summarizeerror=ProcessingError.PARALLEL_TOO_SHORT
            # Was "skipped"
            missingnodes = job.nodecount
            logging.info("Skipping %s, skipped_parallel_too_short", job.job_id)
        elif job.walltime <= 180:
            mergeresult = 1
            mdata["skipped_too_short"] = True
            summarizeerror=ProcessingError.TIME_TOO_SHORT
            missingnodes = job.nodecount
            logging.info("Skipping %s, skipped_too_short", job.job_id)
        elif job.nodecount < 1:
            mergeresult = 1
            mdata["skipped_invalid_nodecount"] = True
            summarizeerror=ProcessingError.INVALID_NODECOUNT
            missingnodes = job.nodecount
            logging.info("Skipping %s, skipped_invalid_nodecount", job.job_id)
        elif not job.has_any_archives():
            mergeresult = 1
            mdata["skipped_noarchives"] = True
            summarizeerror=ProcessingError.NO_ARCHIVES
            missingnodes = job.nodecount
            logging.info("Skipping %s, skipped_noarchives", job.job_id)
        elif not job.has_enough_raw_archives():
            mergeresult = 1
            mdata["skipped_rawarchives"] = True
            summarizeerror=ProcessingError.RAW_ARCHIVES
            missingnodes = job.nodecount
            logging.info("Skipping %s, skipped_rawarchives", job.job_id)
        elif opts['max_nodes'] > 0 and job.nodecount > opts['max_nodes']:
            mergeresult = 1
            mdata["skipped_job_too_big"] = True
            summarizeerror=ProcessingError.JOB_TOO_BIG
            missingnodes = job.nodecount
            logging.info("Skipping %s, skipped_job_too_big", job.job_id)
        elif job.walltime >= 176400:
            mergeresult = 1
            mdata["skipped_too_long"] = True
            summarizeerror=ProcessingError.TIME_TOO_LONG
            missingnodes = job.nodecount
            logging.info("Skipping %s, skipped_too_long", job.job_id)
        else:
            mergeresult = extract_and_merge_logs(job, conf, resconf, opts)
            missingnodes = -1.0 * mergeresult
        mergeend = time.time()

        if opts['extractonly']: 
            return 0 == mergeresult

        preprocessors = [x(job) for x in preprocs]
        analytics = [x(job) for x in plugins]
        s = Summarize(preprocessors, analytics, job, conf)

        enough_nodes=False

        if 0 == mergeresult or ( job.nodecount != 0 and (missingnodes / job.nodecount < 0.05)):
            enough_nodes=True
            logging.info("Success for %s files in %s (%s/%s)", job.job_id, job.jobdir, missingnodes, job.nodecount)
            s.process()
        elif summarizeerror == None and job.nodecount != 0 and (missingnodes / job.nodecount >= 0.05):
            # Don't overwrite existing error
            # Don't have enough node data to even try summarization
            mdata["skipped_pmlogextract_error"] = True
            logging.info("Skipping %s, skipped_pmlogextract_error", job.job_id)
            summarizeerror=ProcessingError.PMLOGEXTRACT_ERROR

        mdata["mergetime"] = mergeend - mergestart
        
        if opts['tag'] != None:
            mdata['tag'] = opts['tag']

        if missingnodes > 0:
            mdata['missingnodes'] = missingnodes

        m.process(s, mdata)

        success = s.good_enough()

        if not success and enough_nodes:
            # We get here if the pmlogextract step gave us enough nodes but summarization didn't succeed for enough nodes
            # All other "known" errors should already be handled above.
            mdata["skipped_summarization_error"] = True
            logging.info("Skipping %s, skipped_summarization_error", job.job_id)
            summarizeerror=ProcessingError.SUMMARIZATION_ERROR

        force_success = False
        if not success:
            force_timeout = opts['force_timeout']
            if (datetime.datetime.now() - job.end_datetime) > datetime.timedelta(seconds=force_timeout):
                force_success = True
                #Need some logic here to figure out which types of errors we want to force_success on. Then can uncomment the below.
                #summarizeerror = None

        dblog.markasdone(job, success or force_success, time.time() - mergestart, summarizeerror)

    except Exception as e:
        logging.error("Failure for job %s %s. Error: %s %s", job.job_id, job.jobdir, str(e), traceback.format_exc())

    if opts['dodelete'] and job.jobdir != None and os.path.exists(job.jobdir):
        # Clean up
        shutil.rmtree(job.jobdir)

    return success

def override_defaults(resconf, opts):
    """ Commandline options that override the configruation file settings """
    if 'job_output_dir' in opts and opts['job_output_dir'] != None:
        resconf['job_output_dir'] = opts['job_output_dir']

    return resconf

def filter_plugins(resconf, preprocs, plugins):
    """ Filter the list of plugins/preprocs to use on a resource basis """

    # Default is to use all
    filtered_preprocs=preprocs
    filtered_plugins=plugins

    if "plugin_whitelist" in resconf:
       filtered_preprocs = [x for x in preprocs if x.__name__ in resconf['plugin_whitelist']]
       filtered_plugins = [x for x in plugins if x.__name__ in resconf['plugin_whitelist']]
    elif "plugin_blacklist" in resconf:
       filtered_preprocs = [x for x in preprocs if x.__name__ not in resconf['plugin_blacklist']]
       filtered_plugins = [x for x in plugins if x.__name__ not in resconf['plugin_blacklist']]

    return filtered_preprocs, filtered_plugins

def processjobs(config, opts, procid, comm):
    """ main function that does the work. One run of this function per process """

    allpreprocs = loadpreprocessors()
    logging.debug("Loaded %s preprocessors", len(allpreprocs))

    allplugins = loadplugins()
    logging.debug("Loaded %s plugins", len(allplugins))

    for r, resconf in config.resourceconfigs():
        if opts['resource'] == None or opts['resource'] == r or opts['resource'] == str(resconf['resource_id']):
            logging.info("Processing resource %s", r)
        else:
            continue

        preprocs, plugins = filter_plugins(resconf, allpreprocs, allplugins)

        logging.debug("Using %s preprocessors", len(preprocs))
        logging.debug("Using %s plugins", len(plugins))


        resconf = override_defaults(resconf, opts)

        with outputter.factory(config, resconf) as m:

            if resconf['batch_system'] == "XDMoD":
                dbif = XDMoDAcct(resconf['resource_id'], config, None, None)
            else:
                dbif = DbAcct(resconf['resource_id'], config)

            list_procs=0

            # Master
            if procid==0:
                getjobs={}
                if opts['mode'] == "single":
                    getjobs['cmd']=dbif.getbylocaljobid
                    getjobs['opts']=[opts['local_job_id'],]
                elif opts['mode'] == "timerange":
                    getjobs['cmd']=dbif.getbytimerange
                    getjobs['opts']=[opts['start'], opts['end'], opts]
                else:
                    getjobs['cmd']=dbif.get
                    getjobs['opts']=[None, None]

                logging.debug("MASTER STARTING")
                numworkers = opts['threads']-1
                numsent = 0
                numreceived = 0


                for job in getjobs['cmd'](*(getjobs['opts'])):
                    if numsent >= numworkers:
                        list_procs += 1
                        if list_procs==1 or list_procs==1000:
                            # Once all ranks are going, dump the process list for debugging
                            logging.info("Dumping process list")
                            allpinfo={}
                            for proc in psutil.process_iter():
                                try:
                                    pinfo = proc.as_dict()
                                except psutil.NoSuchProcess:
                                    pass
                                else:
                                    allpinfo[pinfo['pid']]=pinfo

                            with open("rank-{}_{}.proclist".format(procid, list_procs), 'w') as outfile:
                                json.dump(allpinfo, outfile, indent=2)

                        # Wait for a worker to be done and then send more work
                        process = comm.recv(source=MPI.ANY_SOURCE, tag=1)
                        numreceived += 1
                        comm.send(job, dest=process, tag=1)
                        numsent += 1
                        logging.debug("Sent new job: %d sent, %d received", numsent, numreceived)
                    else:
                        # Initial batch
                        comm.send(job, dest=numsent+1, tag=1)
                        numsent += 1
                        logging.debug("Initial Batch: %d sent, %d received", numsent, numreceived)

                logging.info("After all jobs sent: %d sent, %d received", numsent, numreceived)

                # Get leftover results
                while numsent > numreceived:
                    comm.recv(source=MPI.ANY_SOURCE, tag=1)
                    numreceived += 1
                    logging.debug("Getting leftovers. %d sent, %d received", numsent, numreceived)

                # Shut them down
                for worker in xrange(numworkers):
                    logging.debug("Shutting down: %d", worker+1)
                    comm.send(None, dest=worker+1, tag=1)

            # Worker
            else:
                sendtime=time.time()
                midtime=time.time()
                recvtime=time.time()
                logging.debug("WORKER %d STARTING", procid)
                while True:
                    recvtries=0
                    while not comm.Iprobe(source=0, tag=1):
                        if recvtries < 1000:
                            recvtries+=1
                            continue
                        # Sleep so we can instrument how efficient we are
                        # Otherwise, workers spin on exit at the hidden mpi_finalize call.
                        # If you care about maximum performance and don't care about wasted cycles, remove the Iprobe/sleep loop
                        # Empirically, a tight loop with time.sleep(0.001) uses ~1% CPU
                        time.sleep(0.001)
                    job = comm.recv(source=0, tag=1)
                    recvtime=time.time()
                    mpisendtime = midtime-sendtime
                    mpirecvtime = recvtime-midtime
                    if (mpisendtime+mpirecvtime) > 2:
                        logging.warning("MPI send/recv took %s/%s", mpisendtime, mpirecvtime)
                    if job != None:
                        logging.debug("Rank: %s, Starting: %s", procid, job.job_id)
                        summarizejob(job, config, resconf, plugins, preprocs, m, dbif, opts)
                        logging.debug("Rank: %s, Finished: %s", procid, job.job_id)
                        sendtime=time.time()
                        comm.send(procid, dest=0, tag=1)
                        midtime=time.time()

                        list_procs += 1
                        if list_procs==1 or list_procs==10:
                            # Once all ranks are going, dump the process list for debugging
                            logging.info("Dumping process list")
                            allpinfo={}
                            for proc in psutil.process_iter():
                                try:
                                    pinfo = proc.as_dict()
                                except psutil.NoSuchProcess:
                                    pass
                                else:
                                    allpinfo[pinfo['pid']]=pinfo

                            with open("rank-{}_{}.proclist".format(procid, list_procs), 'w') as outfile:
                                json.dump(allpinfo, outfile, indent=2)
                    else:
                        # Got shutdown message
                        break

def main():
    """
    main entry point for script
    """

    comm = MPI.COMM_WORLD

    opts = getoptions()

    opts['threads'] = comm.Get_size()

    logout = "mpiOutput-{}.log".format(comm.Get_rank())

    # For MPI jobs, do something sane with logging.
    setuplogger(logging.ERROR, logout, opts['log'])

    config = Config()

    if comm.Get_size() < 2:
        logging.error("Must run MPI job with at least 2 processes")
        sys.exit(1)

    myhost = MPI.Get_processor_name() 
    logging.info("Nodename: %s", myhost)

    processjobs(config, opts, comm.Get_rank(), comm)

    logging.info("Rank: %s FINISHED", comm.Get_rank())

if __name__ == "__main__":
    main()