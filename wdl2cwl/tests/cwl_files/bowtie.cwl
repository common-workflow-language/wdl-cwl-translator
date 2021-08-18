class: CommandLineTool
id: Bowtie
inputs:
  - id: indexFiles
    type:
      - items: File
        type: array
  - id: seedmms
    default: ''
    type:
      - int
      - 'null'
  - id: seedlen
    default: ''
    type:
      - int
      - 'null'
  - id: k
    default: ''
    type:
      - int
      - 'null'
  - id: samRG
    default: ''
    type:
      - string
      - 'null'
  - id: outputPath
    default: mapped.bam
    type: string
  - id: best
    default: false
    type: boolean
  - id: strata
    default: false
    type: boolean
  - id: allowContain
    default: false
    type: boolean
  - id: picardXmx
    default: 4G
    type: string
  - id: threads
    default: 1
    type: int
  - id: dockerImage
    default: quay.io/biocontainers/mulled-v2-bfe71839265127576d3cd749c056e7b168308d56:1d8bec77b352cdcf3e9ff3d20af238b33ed96eae-0
    type: string
outputs:
  - id: outputBam
    type: File
    outputBinding:
        glob: $(inputs.outputPath)
requirements:
  - class: DockerRequirement
    dockerPull: quay.io/biocontainers/mulled-v2-bfe71839265127576d3cd749c056e7b168308d56:1d8bec77b352cdcf3e9ff3d20af238b33ed96eae-0
  - class: InitialWorkDirRequirement
    listing:
      - entryname: example.sh
        entry: |4

            set -e -o pipefail
            mkdir -p "\$(dirname $(inputs.outputPath))"
            bowtie \
            -q \
            --sam \
            --seedmms $(inputs.seedmms) \
            --seedlen $(inputs.seedlen) \
            -k $(inputs.k) \
            $(inputs["best"] ? "--best" : "") \
            $(inputs["strata"] ? "--strata" : "") \
            $(inputs["allowContain"] ? "--allow-contain" : "") \
            --threads $(inputs.threads) \
            --sam-RG '$(inputs.samRG) \
            $(return inputs["indexFiles"][0].replace("(\.rev)?\.[0-9]\.ebwt$","");) \
            $(inputs.sep=","readsUpstream) \
            $(inputs.sep=","readsDownstream) \
            | picard -Xmx$(inputs.picardXmx) SortSam \
            INPUT=/dev/stdin \
            OUTPUT=$(inputs.outputPath) \
            SORT_ORDER=coordinate \
            CREATE_INDEX=true
  - class: InlineJavascriptRequirement
  - class: ResourceRequirement
    ramMin: |-
        ${
        var unit = inputs["memory"].match(/[a-zA-Z]+/g).join("");
        var value = parseInt(inputs["memory"].match(/[0-9]+/g));
        var memory = "";
        if(unit==="KiB") memory = value/1024;
        else if(unit==="MiB") memory = value;
        else if(unit==="GiB") memory = value*1024;
        else if(unit==="TiB") memory = value*1024*1024;
        else if(unit==="B") memory = value/(1024*1024);
        else if(unit==="KB" || unit==="K") memory = (value*1000)/(1024*1024);
        else if(unit==="MB" || unit==="M") memory = (value*(1000*1000))/(1024*1024);
        else if(unit==="GB" || unit==="G") memory = (value*(1000*1000*1000))/(1024*1024);
        else if(unit==="TB" || unit==="T") memory = (value*(1000*1000*1000*1000))/(1024*1024);
        return parseInt(memory);
        }
  - class: ResourceRequirement
    coresMin: $(inputs.threads)
cwlVersion: v1.2
baseCommand:
  - sh
  - example.sh
