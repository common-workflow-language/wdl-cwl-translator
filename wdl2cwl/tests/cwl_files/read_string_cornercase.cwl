cwlVersion: v1.2
id: ReadStringCornercase
class: CommandLineTool
requirements:
  - class: InitialWorkDirRequirement
    listing:
      - entryname: script.bash
        entry: |4

            cacheInvalidationRandomString=4

            echo Starting checksum generation...

            # calculate hash for alignment positions only (a reduced bam hash)
            calculated_checksum=\$( samtools view -F 256 "$(inputs.bam.path)" | cut -f 1-11 | md5sum | awk '{print $1}' )
            echo Reduced checksum generation complete

            if [ "$calculated_checksum" == "$(inputs.expected_checksum)" ]
            then
                 echo Computed and expected bam hashes match \( "$calculated_checksum" \)
                 printf PASS > result.txt
            else
                 echo Computed \( "$calculated_checksum" \) and expected \( "$(inputs.expected_checksum)" \) bam file hashes do not match
                 printf FAIL > result.txt
            fi
  - class: InlineJavascriptRequirement
  - class: NetworkAccess
    networkAccess: true
hints:
  - class: DockerRequirement
    dockerPull: quay.io/humancellatlas/secondary-analysis-samtools:v0.2.2-1.6
  - class: ResourceRequirement
    coresMin: 1
    ramMin: 3576.2786865234375
    outdirMin: $((Math.ceil((function(size_of=0){inputs.bam.forEach(function(element){
        if (element) {size_of += element.size}})}) / 1000^3 * 1.1) ) * 1024)
inputs:
  - id: bam
    type: File
  - id: expected_checksum
    type: string
baseCommand:
  - bash
  - script.bash
outputs:
  - id: result
    type: File
    outputBinding:
        glob: read_string.txt
