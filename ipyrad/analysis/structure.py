#!/usr/bin/env python2

""" convenience wrappers for running structure in a jupyter notebook"""

import os
import glob
import subprocess
import itertools
import sys
import pandas as pd
import numpy as np
from numba import jit
from ipyrad.analysis.tetrad import get_spans


# pylint: disable=W0142
# pylint: disable=W0212
# pylint: disable=C0301


class _Object(object):
    """ a custom object with getter and repr, but no getkeys setkeys """

    def __getitem__(self, key):
        return self.__dict__[key]

    def __repr__(self):
        _repr = ""
        keys = sorted(self.__dict__.keys())
        _printstr = "{:<" + str(2 + max([len(i) for i in keys]))+"} {:>10}\n"
        for key in keys:
            _repr += _printstr.format(key, str(self[key]))
        return _repr



## These are almost all default values.
class Structure(object):
    """ 
    ipyrad analysis Structure Object. This is a wrapper to allow easily
    entering parameter setting to submit structure jobs to run in parallel.
    """
    def __init__(self, name, strfile, workdir=None, mapfile=None):
        self.name = name
        self.strfile = os.path.realpath(strfile)
        self.mainparams = _MainParams()
        self.extraparams = _ExtraParams()
        self.clumppparams = _ClumppParams()

        ## make workdir if it does not exist
        if workdir:
            self.workdir = os.path.realpath(workdir)
        else:
            self.workdir = os.path.join(os.path.curdir, "analysis-structure")
        if not os.path.exists(self.workdir):
            os.makedirs(self.workdir)

        ## check that strfile exists, print and parse some info from it
        with open(strfile) as ifile:
            lines = ifile.readlines()
            self.ntaxa = len(lines)//2
            self.nsites = len(lines[0].strip().split()[1:])
            self.labels = [i.split('\t')[0].strip() for i in lines][::2]
            self.popdata = [i.split('\t')[1] for i in lines][::2]
            self.popflag = [i.split('\t')[2] for i in lines][::2]
            del lines

        ## if mapfile then parse it to an array
        if mapfile:
            with open(mapfile) as inmap:
                maparr = np.genfromtxt(inmap)[:, [0, 3]].astype(np.uint64)
                spans = np.zeros((maparr[-1, 0], 2), np.uint64)
                spans = get_spans(maparr, spans)
                self.maparr = spans
                self.nsites = spans.shape[0]
        else:
            self.maparr = None
            

    def _subsample(self):
        """ returns a subsample of unlinked snp sites """
        spans = self.maparr
        samp = np.zeros(spans.shape[0], dtype=np.uint64)
        for i in xrange(spans.shape[0]):
            samp[i] = np.random.randint(spans[i, 0], spans[i, 1], 1)
        return samp


    @property
    def header(self):
        header = pd.DataFrame(
            [self.labels, self.popdata, self.popflag], 
            index=["labels", "popdata", "popflag"]).T
        return header

    @property
    def result_files(self):
        """ returns a list of files that have finished structure """
        reps = os.path.join(self.workdir, self.name+"-K-*-rep-*_f")
        repfiles = glob.glob(reps)
        return repfiles


    def submit_structure_jobs(self,
        kpop, 
        nreps, 
        ipyclient=None,
        seed=12345, 
        quiet=False, 
        return_asyncs=False, 
        ):

        """ 
        submits a job to run on the cluster and returns an asynchronous result
        object. K is the number of populations, randomseed if not set will be 
        randomly drawn, ipyclient if not entered will raise an error. 

        Parameters:
        -----------
        kpop: (int)
            The MAXPOPS parameter in structure, i.e., the number of populations
            assumed by the model (K). 

        nreps: (int):
            Number of independent runs starting from distinct seeds.

        ipyclient: (ipyparallel.Client Object)
            An ipyparallel client connected to an ipcluster instance. This is 
            used to manage parallel jobs. If not present a single job will
            run and block until finished (i.e., code is not parallel).

        seed: (int):
            Random number seed used for subsampling unlinked SNPs if a mapfile
            is linked to the Structure Object. 

        quiet: (bool)
            Whether to print number of jobs submitted to stderr

        return_asyncs: (bool):
            Whether to return the asynchronous result objects for each job. The
            default is False. These can be used for debugging if a job fails.

        Returns: 
        ---------
        An ipyparallel or multiprocessing asynchronous result object. 

        Example: 
        ---------
        import ipyparallel as ipp
        from ipyrad.analysis import Structure

        ## get parallel client
        ipyclient = ipp.Client()

        ## get structure object
        s = Structure(strfile="mydata.str", 
                      workdir="structure-results",
                      mapfile="mydata.snps.map")

        ## set some basic params
        s.mainparams.numreps = 100000
        s.mainparams.burnin = 10000

        ## submit many jobs
        for kpop in [3, 4, 5]:
            s.submit_structure_jobs(kpop=kpop, nreps=10, ipyclient=ipyclient)

        ## track progress


        """
        ## initiate seed
        np.random.seed(seed)

        ## prepare files
        self._prepare_structure_files()

        ## check that there is a ipcluster instance running
        asyncs = []
        for rep in xrange(nreps):
            self.extraparams.seed = np.random.randint(0, 1e9, 1)[0]
            if ipyclient:
                ## call structure        
                lbview = ipyclient.load_balanced_view()
                async = lbview.apply(_call_structure, *(self, kpop, rep))
                asyncs.append(async)

            else:
                sys.stderr.write("submitted 1 structure job [{}-K-{}]\n"\
                                 .format(self.name, kpop))
                comm = _call_structure(self, kpop, rep)
                return comm

        if ipyclient:
            if not quiet:
                sys.stderr.write("submitted {} structure jobs [{}-K-{}]\n"\
                                .format(nreps, self.name, kpop))
            if return_asyncs:
                return asyncs



    def _prepare_structure_files(self):
        """ prepares input files for running structure"""

        ## remove old jobs with this same name
        handle = os.path.join(self.workdir, self.name+"-K-*")
        oldjobs = glob.glob(handle)
        for job in oldjobs:
            os.remove(job)

        ## check params
        self.mainparams.numreps = int(self.mainparams.numreps)
        self.mainparams.burnin = int(self.mainparams.burnin)

        ## write tmp files for the job
        tmp_m = open(os.path.join(self.workdir, "tmp.mainparams.txt"), 'w')
        tmp_e = open(os.path.join(self.workdir, "tmp.extraparams.txt"), 'w')
        tmp_s = open(os.path.join(self.workdir, "tmp.strfile.txt"), 'w')

        ## write params files
        tmp_m.write(self.mainparams._asfile())
        tmp_e.write(self.extraparams._asfile())

        ## subsample SNPs as unlinked if a mapfile is present.
        ## & write pop data to the tmp_s file if present
        assert len(self.popdata) == len(self.labels), \
            "popdata list must be the same length as the number of taxa"

        with open(self.strfile) as ifile:
            _data = ifile.readlines()
            ## header
            header = np.array([i.strip().split("\t")[:5] for i in _data])
            ## seqdata
            seqdata = np.array([i.strip().split("\t")[5:] for i in _data])

            ## enter popdata into seqfile if present in self
            if any(self.popdata):
                ## set popdata in header
                header[::2, 1] = self.popdata
                header[1::2, 1] = self.popdata
                
                ## set flag to all 1s if user entered popdata but no popflag
                if not any(self.popflag):
                    self.popflag = [1 for i in self.popdata]
                    header[:, 2] = 1
                else:
                    header[::2, 2] = self.popdata
                    header[1::2, 2] = self.popdata

            ## subsample SNPs if mapfile is present
            if isinstance(self.maparr, np.ndarray):
                seqdata = seqdata[:, self._subsample()]
                
            ## write fullstr
            fullstr = np.concatenate([header, seqdata], axis=1)
            np.savetxt(tmp_s, fullstr, delimiter="\t", fmt="%s")

        ## close tmp files
        tmp_m.close()
        tmp_e.close()
        tmp_s.close()


    def get_clumpp_table(self, kpop):
        table = _get_clumpp_table(self, kpop)
        return table



def _call_structure(sobj, kpop, rep):
    """ make the subprocess call to structure """
    ## create call string
    outname = os.path.join(sobj.workdir, 
                "{}-K-{}-rep-{}".format(sobj.name, kpop, rep))
    cmd = ["structure", 
           "-m", os.path.join(sobj.workdir, "tmp.mainparams.txt"),
           "-e", os.path.join(sobj.workdir, "tmp.extraparams.txt"),
           "-K", str(kpop),
           "-D", str(sobj.extraparams.seed), 
           "-N", str(sobj.ntaxa), 
           "-L", str(sobj.nsites),
           "-i", os.path.join(sobj.workdir, "tmp.strfile.txt"),
           "-o", outname]

    ## call the shell function
    proc = subprocess.Popen(cmd,
                            stdout=subprocess.PIPE, 
                            stderr=subprocess.STDOUT)
    comm = proc.communicate()
    return comm



class _MainParams(_Object):
    def __init__(self):
        self.burnin = int(250000)
        self.numreps = int(1e6)
        self.ploidy = 2
        self.missing = -9
        self.onerowperind = 0
        self.label = 1
        self.popdata = 0
        self.popflag = 0
        self.locdata = 0
        self.phenotype = 0
        self.extracols = 0
        self.markernames = 0
        self.recessivealleles = 0
        self.mapdistances = 0
        self.phased = 0
        self.phaseinfo = 0
        self.markovphase = 0
        self.notambiguous = -999

    def _asfile(self):
        return _MAINPARAMS.format(**self.__dict__)



class _ExtraParams(_Object):

    def __init__(self):
        self.noadmix = 0
        self.linkage = 0
        self.usepopinfo = 0
        self.locprior = 0
        self.freqscorr = 1
        self.onefst = 0
        self.inferalpha = 1
        self.popalphas = 0
        self.alpha = 1.0

        self.inferlambda = 0
        self.popspecificlambda = 0
        self.lambda_ = 1.0

        self.fpriormean = 0.01
        self.fpriorsd = 0.05
        self.unifprioralpha = 1
        self.alphamax = 10.0
        self.alphapriora = 1.0
        self.alphapriorb = 2.0
        self.log10rmin = -4.0
        self.log10rmax = 1.0
        self.log10rpropsd = 0.1
        self.log10rstart = -2.0

        self.gensback = 2
        self.migrprior = 0.01
        self.pfrompopflagonly = 0

        self.locispop = 0
        self.locpriorinit = 1.0
        self.maxlocprior = 20.0

        self.printnet = 1               ## do we want these to print ?
        self.printlambda = 1            ##
        self.printqsum = 1              ##
        self.sitebysite = 0
        self.printqhat = 0
        self.updatefreq = 10000
        self.printlikes = 0
        self.intermedsave = 0
        self.echodata = 0       
        self.ancestdist = 0
        self.numboxes = 1000
        self.ancestpint = 0.90

        self.computeprob = 1
        self.admburnin = 500
        self.alphapropsd = 0.025
        self.startatpopinfo = 0
        self.randomize = 0
        self.seed = 12345
        self.metrofreq = 10
        self.reporthitrate = 0


    def _asfile(self):
        return _EXTRAPARAMS.format(**self.__dict__)



class _ClumppParams(_Object):
    def __init__(self):
        self.datatype = 0
        self.indfile = 0
        self.outfile = 0
        self.popfile = 0
        self.miscfile = 0
        #self.kpop = 3
        #self.c = 3
        #self.r = 10
        self.m = 2
        self.w = 1
        self.s = 2
        self.greedy_option = 2
        self.repeats = 1000
        self.permutationsfile = 0
        self.print_permuted_data = 0
        self.permuted_datafile = 0
        self.print_every_perm = 0
        self.every_permfile = 0
        self.permfile = 0
        self.print_random_inputorder = 0
        self.random_inputorderfile = 0
        self.override_warnings = 0
        self.order_by_run = 1


    def _asfile(self):
        return _CLUMPPARAMS.format(**self.__dict__)



def _get_clumpp_table(self, kpop):

    ## concat results for k=x
    reps = _concat_reps(self, kpop)
    if reps:
        ninds = reps[0].inds
        nreps = len(reps)
    else:
        ninds = nreps = 0

    clumphandle = os.path.join(self.workdir, "tmp.clumppparams.txt")
    self.clumppparams.kpop = kpop
    self.clumppparams.c = ninds
    self.clumppparams.r = nreps
    with open(clumphandle, 'w') as tmp_c:
        tmp_c.write(self.clumppparams._asfile())
    
    ## create CLUMPP args string
    outfile = os.path.join(self.workdir, 
                "{}-K-{}.outfile".format(self.name, kpop))
    indfile = os.path.join(self.workdir, 
                "{}-K-{}.indfile".format(self.name, kpop))
    miscfile = os.path.join(self.workdir, 
                "{}-K-{}.miscfile".format(self.name, kpop))
    cmd = ["CLUMPP", clumphandle, 
           "-i", indfile,
           "-o", outfile, 
           "-j", miscfile,
           "-r", str(nreps), 
           "-c", str(ninds), 
           "-k", str(kpop)]

    ## call clumpp
    proc = subprocess.Popen(cmd, 
                            stderr=subprocess.STDOUT, 
                            stdout=subprocess.PIPE)
    _ = proc.communicate()

    ## cleanup
    for rfile in [indfile, miscfile]:
        if os.path.exists(rfile):
            os.remove(rfile)

    ## parse clumpp results file
    ofile = os.path.join(self.workdir, "{}-K-{}.outfile".format(self.name, kpop))
    if os.path.exists(ofile):
        csvtable = pd.read_csv(ofile, delim_whitespace=True, header=None)
        table = csvtable.ix[:, 5:]
    
        ## apply names to cols and rows
        table.columns = range(table.shape[1])
        table.index = self.labels
        sys.stderr.write("mean scores across {} replicates.\n".format(nreps))
        return table

    else:
        sys.stderr.write("No _f files ready in {}\n".format(self.workdir))
        return 



def _concat_reps(self, kpop):
    "combine replicates into single indfile, returns nreps, ninds"
   
    ## make an output handle
    outf = os.path.join(self.workdir, 
        "{}-K-{}.indfile".format(self.name, kpop))
    
    ## combine replicates and write to indfile
    reps = []
    with open(outf, 'w') as outfile:
        for rep in self.result_files:
            result = Rep(rep)
            reps.append(result)
            outfile.write(result.stable)
    return reps



# class Clumpp(object):
#     """ results from a clumpp of structure results """
#     def __init__(self):
#         self.nreps = 0
#         self.table = None
#         self.meanLK = 0
#         self.LppK = 0
#         self.evanno


#         self.LnPK
#         self.LnPPK
#         self.deltaK



class Rep(object):
    """ parsed structure result file object """
    def __init__(self, repfile):
        self.repfile = repfile
        self.est_lnlik = 0
        self.mean_lnlik = 0
        self.var_lnlik = 0
        self.alpha = 0
        self.inds = 0
        
        ## get table string        
        self.stable = self.parse()
        

    def parse(self):
        """ parse an _f structure output file """
        stable = ""
        with open(self.repfile) as orep:
            dat = orep.readlines()
            for line in dat:
                ## stat lines
                if "Estimated Ln Prob of Data" in line:
                    self.est_lnlik = float(line.split()[-1])
                if "Mean value of ln likelihood" in line:
                    self.mean_lnlik = float(line.split()[-1])
                if "Variance of ln likelihood" in line:
                    self.var_lnlik = float(line.split()[-1])
                if "Mean value of alpha" in line:
                    self.alpha = float(line.split()[-1])

                ## matrix lines
                if ")   :  " in line:
                    ## check if sample is supervised...
                    abc = line.strip().split()
                    outstr = "{}{}{}".format(
                        " ".join([abc[0], abc[0], abc[2], 
                                  abc[0].split('.')[0]]),
                        " :  ",
                        " ".join(abc[4:])
                    )
                    self.inds += 1
                    stable += outstr+"\n"
            stable += "\n"
        return stable




_MAINPARAMS = """
#define BURNIN         {burnin}                   //
#define NUMREPS        {numreps}                  //

#define PLOIDY         {ploidy}                   //
#define MISSING        {missing}                  //
#define ONEROWPERIND   {onerowperind}              //     

#define LABEL          {label}                     //
#define POPDATA        {popdata}                   //
#define POPFLAG        {popflag}                   //
#define LOCDATA        {locdata}                   //
#define PHENOTYPE      {phenotype}                 //
#define EXTRACOLS      {extracols}                 //

#define MARKERNAMES       {markernames}           //
#define RECESSIVEALLELES  {recessivealleles}      //
#define MAPDISTANCES      {mapdistances}          //
#define PHASED            {phased}                //
#define PHASEINFO         {phaseinfo}             //
#define MARKOVPHASE       {markovphase}           //
#define NOTAMBIGUOUS      {notambiguous}          //
"""


_EXTRAPARAMS = """
#define NOADMIX             {noadmix}                    //
#define LINKAGE             {linkage}                    //
#define USEPOPINFO          {usepopinfo}                 //
#define LOCPRIOR            {locprior}                   //
#define FREQSCORR           {freqscorr}                  //
#define ONEFST              {onefst}                     //
#define INFERALPHA          {inferalpha}                 // 
#define POPALPHAS           {popalphas}                  // 
#define ALPHA               {alpha}                      // 

#define INFERLAMBDA         {inferlambda}                  //
#define POPSPECIFICLAMBDA   {popspecificlambda}            //
#define LAMBDA              {lambda_}                       // 
             
#define FPRIORMEAN          {fpriormean}                   //
#define FPRIORSD            {fpriorsd}                     // 
#define UNIFPRIORALPHA      {unifprioralpha}               // 
#define ALPHAMAX            {alphamax}                     // 
#define ALPHAPRIORA         {alphapriora}                  // 
#define ALPHAPRIORB         {alphapriorb}                  //
#define LOG10RMIN           {log10rmin}                    //
#define LOG10RMAX           {log10rmax}                    //
#define LOG10RPROPSD        {log10rpropsd}                 //
#define LOG10RSTART         {log10rstart}                  //

#define GENSBACK            {gensback}                      //
#define MIGRPRIOR           {migrprior}                      //
#define PFROMPOPFLAGONLY    {pfrompopflagonly}              //

#define LOCISPOP            {locispop}                      //
#define LOCPRIORINIT        {locpriorinit}                  //
#define MAXLOCPRIOR         {maxlocprior}                   //

#define PRINTNET            {printnet}                     // 
#define PRINTLAMBDA         {printlambda}                  //
#define PRINTQSUM           {printqsum}                    //
#define SITEBYSITE          {sitebysite}                   // 
#define PRINTQHAT           {printqhat}                    // 
#define UPDATEFREQ          {updatefreq}                   // 
#define PRINTLIKES          {printlikes}                   // 
#define INTERMEDSAVE        {intermedsave}                 //
#define ECHODATA            {echodata}                     // 
#define ANCESTDIST          {ancestdist}                   // 
#define NUMBOXES            {numboxes}                     // 
#define ANCESTPINT          {ancestpint}                   // 

#define COMPUTEPROB         {computeprob}                    //
#define ADMBURNIN           {admburnin}                      // 
#define ALPHAPROPSD         {alphapropsd}                    // 
#define STARTATPOPINFO      {startatpopinfo}                 //
#define RANDOMIZE           {randomize}                      //
#define SEED                {seed}                           //
#define METROFREQ           {metrofreq}                      // 
#define REPORTHITRATE       {reporthitrate}                  //  

"""

_CLUMPPARAMS = """
DATATYPE                      {datatype}               #
INDFILE                       {indfile}                #
POPFILE                       {popfile}                #
OUTFILE                       {outfile}                #
MISCFILE                      {miscfile}               #
K                             {kpop}                   #
C                             {c}                      #
R                             {r}                      # 
M                             {m}                      #
W                             {w}                      #
S                             {s}                      # 
GREEDY_OPTION                 {greedy_option}          #
REPEATS                       {repeats}                #
PERMUTATIONFILE               {permutationsfile}       #
PRINT_PERMUTED_DATA           {print_permuted_data}    #
PERMUTED_DATAFILE             {permuted_datafile}      #
PRINT_EVERY_PERM              {print_every_perm}       #
EVERY_PERMFILE                {every_permfile}             #
PRINT_RANDOM_INPUTORDER       {print_random_inputorder}    # 
RANDOM_INPUTORDERFILE         {random_inputorderfile}      #
OVERRIDE_WARNINGS             {override_warnings}          #
ORDER_BY_RUN                  {order_by_run}               #
"""
