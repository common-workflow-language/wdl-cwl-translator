class: CommandLineTool
id: HaplotypeCaller
inputs:
  - id: inputBams
    type:
      - items: File
        type: array
  - id: inputBamsIndex
    type:
      - items: File
        type: array
  - id: outputPath
    type: string
  - id: referenceFasta
    type: File
  - id: referenceFastaIndex
    type: File
  - id: referenceFastaDict
    type: File
  - id: intervalList
    default: []
    type:
      - items:
          - File
          - 'null'
        type: array
  - id: excludeIntervalList
    default: []
    type:
      - items:
          - File
          - 'null'
        type: array
  - id: contamination
    default: ''
    type:
      - float
      - 'null'
  - id: dbsnpVCF
    default: ''
    type:
      - File
      - 'null'
  - id: dbsnpVCFIndex
    default: ''
    type:
      - File
      - 'null'
  - id: pedigree
    default: ''
    type:
      - File
      - 'null'
  - id: ploidy
    default: ''
    type:
      - int
      - 'null'
  - id: outputMode
    default: ''
    type:
      - string
      - 'null'
  - id: standardMinConfidenceThresholdForCalling
    default: ''
    type:
      - float
      - 'null'
  - id: gvcf
    default: false
    type: boolean
  - id: dontUseSoftClippedBases
    default: false
    type: boolean
  - id: javaXmxMb
    default: 4096
    type: int
  - id: timeMinutes
    default: 400
    type: int
  - id: dockerImage
    default: quay.io/biocontainers/gatk4:4.1.8.0--py38h37ae868_0
    type: string
outputs:
  - id: outputVCF
    type: File
    outputBinding:
        glob: $(inputs.outputPath)
  - id: outputVCFIndex
    type: File
    outputBinding:
        glob: $(inputs.outputPath).tbi
requirements:
  - class: DockerRequirement
    dockerPull: quay.io/biocontainers/gatk4:4.1.8.0--py38h37ae868_0
  - class: InitialWorkDirRequirement
    listing:
      - entryname: example.sh
        entry: |4

            set -e
            mkdir -p "\$(dirname $(inputs.outputPath))"
            gatk --java-options '-Xmx$(inputs.javaXmxMb)M -XX:ParallelGCThreads=1' \
            HaplotypeCaller \
            -R $(inputs.referenceFasta.path) \
            -O $(inputs.outputPath) \
            -I ${
            var text = "";
            for(var i=0;i<inputs["inputBams"].length;i++) 
              text+= inputs["inputBams"][i].path+" -I ";
            return text;
            } \
            --sample-ploidy $(inputs.ploidy) \
            $(inputs["intervalList"].length === 0 ? "": "-L") ${
            var text = "";
            for(var i=0;i<inputs["intervalList"].length;i++) 
              text+= inputs["intervalList"][i].path+" -L ";
            return text;
            } \
            $(inputs["excludeIntervalList"].length === 0 ? "": "-XL") ${
            var text = "";
            for(var i=0;i<inputs["excludeIntervalList"].length;i++) 
              text+= inputs["excludeIntervalList"][i].path+" -XL ";
            return text;
            } \
            -D $(inputs.dbsnpVCF) \
            --pedigree $(inputs.pedigree) \
            --contamination-fraction-per-sample-file $(inputs.contamination) \
            --output-mode $(inputs.outputMode) \
            --emit-ref-confidence $(inputs.emitRefConfidence) \
            $(inputs["dontUseSoftClippedBases"] ? "--dont-use-soft-clipped-bases" : "") \
            --standard-min-confidence-threshold-for-calling $(inputs.standardMinConfidenceThresholdForCalling)
  - class: InlineJavascriptRequirement
  - class: ToolTimeLimit
    timelimit: $(inputs.timeMinutes* 60)
cwlVersion: v1.2
baseCommand:
  - sh
  - example.sh
