FROM maven:3.9.9-eclipse-temurin-11 AS plugin-build

WORKDIR /src
COPY payment-reference/adapters/killbill-payment-sandbox/pom.xml ./pom.xml
COPY payment-reference/adapters/killbill-payment-sandbox/src ./src
RUN mvn -B -ntp verify \
    && test -f target/killbill-payment-sandbox-plugin-0.1.0-SNAPSHOT.jar

FROM killbill/killbill:0.24.22@sha256:e135615bd14f8ff52d258a4b6a936b4cd0276e30a874a7e2c4690f7a1e28f7f1

COPY --from=plugin-build /src/target/killbill-payment-sandbox-plugin-0.1.0-SNAPSHOT.jar /tmp/killbill-payment-sandbox-plugin-0.1.0-SNAPSHOT.jar

RUN "$KPM_INSTALL_DIR/kpm-latest/kpm" install_java_plugin payment-sandbox \
      --from-source-file=/tmp/killbill-payment-sandbox-plugin-0.1.0-SNAPSHOT.jar \
      --version=0.1.0-SNAPSHOT \
      --destination="$KILLBILL_INSTALL_DIR/bundles"
