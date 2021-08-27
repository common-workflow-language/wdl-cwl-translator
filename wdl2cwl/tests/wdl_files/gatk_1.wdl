version 1.0

# Source: https://github.com/biowdl/tasks/blob/bc1bacf11498d2d30b85591cfccdcf71ef0966a5/gatk.wdl#L980
#
# Copyright (c) 2018 Leiden University Medical Center
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

task HaplotypeCaller {
    input {
        Array[File]+ inputBams
        Array[File]+ inputBamsIndex
        String outputPath
        File referenceFasta
        File referenceFastaIndex
        File referenceFastaDict
        Boolean gvcf = false
        #String emitRefConfidence = if gvcf then "GVCF" else "NONE"
        Boolean dontUseSoftClippedBases = false

        Array[File]+? intervalList
        Array[File]+? excludeIntervalList
        Float? contamination
        File? dbsnpVCF
        File? dbsnpVCFIndex
        File? pedigree
        Int? ploidy
        String? outputMode
        Float? standardMinConfidenceThresholdForCalling

        Int javaXmxMb = 4096
        # Memory increases with time used. 4G should cover most use cases.
        #Int memoryMb = javaXmxMb + 512
        Int timeMinutes = 400 # This will likely be used with intervals, as such size based estimation can't be used.
        String dockerImage = "quay.io/biocontainers/gatk4:4.1.8.0--py38h37ae868_0"
    }

    command {
        set -e
        mkdir -p "$(dirname ~{outputPath})"
        gatk --java-options '-Xmx~{javaXmxMb}M -XX:ParallelGCThreads=1' \
        HaplotypeCaller \
        -R ~{referenceFasta} \
        -O ~{outputPath} \
        -I ~{sep=" -I " inputBams} \
        ~{"--sample-ploidy " + ploidy} \
        ~{true="-L" false="" defined(intervalList)} ~{sep=' -L ' intervalList} \
        ~{true="-XL" false="" defined(excludeIntervalList)} ~{sep=' -XL ' excludeIntervalList} \
        ~{"-D " + dbsnpVCF} \
        ~{"--pedigree " + pedigree} \
        ~{"--contamination-fraction-per-sample-file " + contamination} \
        ~{"--output-mode " + outputMode} \
        --emit-ref-confidence ~{emitRefConfidence} \
        ~{true="--dont-use-soft-clipped-bases" false="" dontUseSoftClippedBases} \
        ~{"--standard-min-confidence-threshold-for-calling " + standardMinConfidenceThresholdForCalling}
    }

    output {
        File outputVCF = outputPath
        File outputVCFIndex = outputPath + ".tbi"
    }

    runtime {
        #memory: "~{memoryMb}M"
        time_minutes: timeMinutes
        docker: dockerImage
    }

    parameter_meta {
        # inputs
        inputBams: {description: "The BAM files on which to perform variant calling.", category: "required"}
        inputBamsIndex: {description: "The indexes for the input BAM files.", category: "required"}
        outputPath: {description: "The location to write the output to.", category: "required"}
        referenceFasta: {description: "The reference fasta file which was also used for mapping.", category: "required"}
        referenceFastaDict: {description: "The sequence dictionary associated with the reference fasta file.", category: "required"}
        referenceFastaIndex: {description: "The index for the reference fasta file.", category: "required"}
        gvcf: {description: "Whether the output should be a gvcf.", category: "common"}
        emitRefConfidence: {description: "Whether to include reference calls. Three modes: 'NONE', 'BP_RESOLUTION' and 'GVCF'.", category: "advanced"}
        dontUseSoftClippedBases: {description: "Do not use soft-clipped bases. Should be 'true' for RNA variant calling.", category: "common"}
        intervalList: {description: "Bed files or interval lists describing the regions to operate on.", category: "common"}
        excludeIntervalList: {description: "Bed files or interval lists describing the regions to NOT operate on.", category: "common"}
        contamination: {description: "Equivalent to HaplotypeCaller's `-contamination` option.", category: "advanced"}
        dbsnpVCF: {description: "A dbSNP VCF.", category: "common"}
        dbsnpVCFIndex: {description: "The index for the dbSNP VCF.", category: "common"}
        pedigree: {description: 'Pedigree file for determining the population "founders".', category: "common"}
        ploidy: {description: "The ploidy with which the variants should be called.", category: "common"}
        outputMode: {description: "Specifies which type of calls we should output. Same as HaplotypeCaller's `--output-mode` option.", category: "advanced"}
        standardMinConfidenceThresholdForCalling: {description: "Confidence threshold used for calling variants.", category: "advanced"}
        javaXmxMb: {description: "The maximum memory available to the program in megabytes. Should be lower than `memoryMb` to accommodate JVM overhead.", category: "advanced"}
        memoryMb: {description: "The amount of memory this job will use in megabytes.", category: "advanced"}
        timeMinutes: {description: "The maximum amount of time the job will run in minutes.", category: "advanced"}
        dockerImage: {description: "The docker image used for this task. Changing this may result in errors which the developers may choose not to address.", category: "advanced"}

        # outputs
        outputVCF: {description: "Raw, unfiltered SNP and indel calls."}
        outputVCFIndex: {description: "Index of output VCF."}
    }
}
