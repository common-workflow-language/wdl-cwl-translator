class: CommandLineTool
id: Format
inputs:
  - id: inputFiles
    type:
      - items: File
        type: array
  - id: format
    default: fasta
    type: string
  - id: outputPath
    default: seq_data.sdf
    type: string
  - id: rtgMem
    default: 8G
    type: string
  - id: memory
    default: 9G
    type: string
  - id: dockerImage
    default: quay.io/biocontainers/rtg-tools:3.10.1--0
    type: string
outputs:
  - id: sdf
    type:
      - items: File
        type: array
    outputBinding:
        glob: $(inputs.outputPath)/*
requirements:
  - class: DockerRequirement
    dockerPull: quay.io/biocontainers/rtg-tools:3.10.1--0
  - class: InitialWorkDirRequirement
    listing:
      - entryname: example.sh
        entry: |4

            set -e
            mkdir -p \$(dirname $(inputs.outputPath))
            rtg RTG_MEM=$(inputs.rtgMem) format -f $(inputs.format) \
            -o $(inputs.outputPath) \
            ${
            var text = "";
            for(var i=0;i<inputs["inputFiles"].length;i++) 
              text+= inputs["inputFiles"][i].path+" ";
            return text;
            }
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
cwlVersion: v1.2
baseCommand:
  - sh
  - example.sh
