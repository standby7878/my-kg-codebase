FROM neo4j:5.26-community

COPY docker/bulk-import.sh /usr/local/bin/codekg-bulk-import

RUN chmod 0555 /usr/local/bin/codekg-bulk-import

ENTRYPOINT ["/usr/local/bin/codekg-bulk-import"]
