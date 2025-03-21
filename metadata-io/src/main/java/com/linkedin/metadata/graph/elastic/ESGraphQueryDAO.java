package com.linkedin.metadata.graph.elastic;

import com.codahale.metrics.Timer;
import com.datahub.util.exception.ESQueryException;
import com.google.common.collect.ImmutableList;
import com.google.common.collect.Lists;
import com.linkedin.common.UrnArray;
import com.linkedin.common.UrnArrayArray;
import com.linkedin.common.urn.Urn;
import com.linkedin.common.urn.UrnUtils;
import com.linkedin.metadata.graph.GraphFilters;
import com.linkedin.metadata.graph.LineageDirection;
import com.linkedin.metadata.graph.LineageRelationship;
import com.linkedin.metadata.models.registry.LineageRegistry;
import com.linkedin.metadata.models.registry.LineageRegistry.EdgeInfo;
import com.linkedin.metadata.query.filter.Condition;
import com.linkedin.metadata.query.filter.ConjunctiveCriterion;
import com.linkedin.metadata.query.filter.Criterion;
import com.linkedin.metadata.query.filter.Filter;
import com.linkedin.metadata.query.filter.RelationshipDirection;
import com.linkedin.metadata.query.filter.RelationshipFilter;
import com.linkedin.metadata.search.utils.ESUtils;
import com.linkedin.metadata.utils.ConcurrencyUtils;
import com.linkedin.metadata.utils.elasticsearch.IndexConvention;
import com.linkedin.metadata.utils.metrics.MetricUtils;
import io.opentelemetry.extension.annotations.WithSpan;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashSet;
import java.util.LinkedList;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.TimeUnit;
import java.util.function.Function;
import java.util.stream.Collectors;
import javax.annotation.Nonnull;
import javax.annotation.Nullable;
import lombok.RequiredArgsConstructor;
import lombok.Value;
import lombok.extern.slf4j.Slf4j;
import org.apache.commons.lang3.tuple.Pair;
import org.elasticsearch.action.search.SearchRequest;
import org.elasticsearch.action.search.SearchResponse;
import org.elasticsearch.client.RequestOptions;
import org.elasticsearch.client.RestHighLevelClient;
import org.elasticsearch.index.query.BoolQueryBuilder;
import org.elasticsearch.index.query.QueryBuilder;
import org.elasticsearch.index.query.QueryBuilders;
import org.elasticsearch.search.SearchHit;
import org.elasticsearch.search.builder.SearchSourceBuilder;

import static com.linkedin.metadata.graph.elastic.ElasticSearchGraphService.*;


/**
 * A search DAO for Elasticsearch backend.
 */
@Slf4j
@RequiredArgsConstructor
public class ESGraphQueryDAO {

  private final RestHighLevelClient client;
  private final LineageRegistry lineageRegistry;
  private final IndexConvention indexConvention;

  private static final int MAX_ELASTIC_RESULT = 10000;
  private static final int BATCH_SIZE = 1000;
  private static final int TIMEOUT_SECS = 10;
  private static final String SOURCE = "source";
  private static final String DESTINATION = "destination";
  private static final String RELATIONSHIP_TYPE = "relationshipType";
  private static final String SEARCH_EXECUTIONS_METRIC = "num_elasticSearch_reads";
  private static final String CREATED_ON = "createdOn";
  private static final String CREATED_ACTOR = "createdActor";
  private static final String UPDATED_ON = "updatedOn";
  private static final String UPDATED_ACTOR = "updatedActor";
  private static final String PROPERTIES = "properties";
  private static final String UI = "UI";

  @Nonnull
  public static void addFilterToQueryBuilder(@Nonnull Filter filter, String node, BoolQueryBuilder rootQuery) {
    BoolQueryBuilder orQuery = new BoolQueryBuilder();
    for (ConjunctiveCriterion conjunction : filter.getOr()) {
      final BoolQueryBuilder andQuery = new BoolQueryBuilder();
      final List<Criterion> criterionArray = conjunction.getAnd();
      if (!criterionArray.stream().allMatch(criterion -> Condition.EQUAL.equals(criterion.getCondition()))) {
        throw new RuntimeException("Currently Elastic query filter only supports EQUAL condition " + criterionArray);
      }
      criterionArray.forEach(
          criterion -> andQuery.must(QueryBuilders.termQuery(node + "." + criterion.getField(), criterion.getValue())));
      orQuery.should(andQuery);
    }
    rootQuery.must(orQuery);
  }

  private SearchResponse executeSearchQuery(@Nonnull final QueryBuilder query, final int offset, final int count) {
    SearchRequest searchRequest = new SearchRequest();

    SearchSourceBuilder searchSourceBuilder = new SearchSourceBuilder();

    searchSourceBuilder.from(offset);
    searchSourceBuilder.size(count);

    searchSourceBuilder.query(query);

    searchRequest.source(searchSourceBuilder);

    searchRequest.indices(indexConvention.getIndexName(INDEX_NAME));

    try (Timer.Context ignored = MetricUtils.timer(this.getClass(), "esQuery").time()) {
      MetricUtils.counter(this.getClass(), SEARCH_EXECUTIONS_METRIC).inc();
      return client.search(searchRequest, RequestOptions.DEFAULT);
    } catch (Exception e) {
      log.error("Search query failed", e);
      throw new ESQueryException("Search query failed:", e);
    }
  }

  private SearchResponse executeSearchQuery(@Nonnull final QueryBuilder query, @Nullable Object[] sort, @Nullable String pitId,
      @Nonnull String keepAlive, final int count) {
    SearchRequest searchRequest = new SearchRequest();

    SearchSourceBuilder searchSourceBuilder = new SearchSourceBuilder();

    ESUtils.setSearchAfter(searchSourceBuilder, sort, pitId, keepAlive);
    searchSourceBuilder.size(count);
    searchSourceBuilder.query(query);

    searchRequest.source(searchSourceBuilder);
    searchRequest.indices(indexConvention.getIndexName(INDEX_NAME));

    try (Timer.Context ignored = MetricUtils.timer(this.getClass(), "esQuery").time()) {
      MetricUtils.counter(this.getClass(), SEARCH_EXECUTIONS_METRIC).inc();
      return client.search(searchRequest, RequestOptions.DEFAULT);
    } catch (Exception e) {
      log.error("Search query failed", e);
      throw new ESQueryException("Search query failed:", e);
    }

  }

  public SearchResponse getSearchResponse(@Nullable final List<String> sourceTypes, @Nonnull final Filter sourceEntityFilter,
      @Nullable final List<String> destinationTypes, @Nonnull final Filter destinationEntityFilter,
      @Nonnull final List<String> relationshipTypes, @Nonnull final RelationshipFilter relationshipFilter,
      final int offset, final int count) {
    BoolQueryBuilder finalQuery =
        buildQuery(sourceTypes, sourceEntityFilter, destinationTypes, destinationEntityFilter, relationshipTypes,
            relationshipFilter);

    return executeSearchQuery(finalQuery, offset, count);
  }

  public static BoolQueryBuilder buildQuery(@Nullable final List<String> sourceTypes, @Nonnull final Filter sourceEntityFilter,
      @Nullable final List<String> destinationTypes, @Nonnull final Filter destinationEntityFilter,
      @Nonnull final List<String> relationshipTypes, @Nonnull final RelationshipFilter relationshipFilter) {
    BoolQueryBuilder finalQuery = QueryBuilders.boolQuery();

    final RelationshipDirection relationshipDirection = relationshipFilter.getDirection();

    // set source filter
    String sourceNode = relationshipDirection == RelationshipDirection.OUTGOING ? SOURCE : DESTINATION;
    if (sourceTypes != null && sourceTypes.size() > 0) {
      finalQuery.must(QueryBuilders.termsQuery(sourceNode + ".entityType", sourceTypes));
    }
    addFilterToQueryBuilder(sourceEntityFilter, sourceNode, finalQuery);

    // set destination filter
    String destinationNode = relationshipDirection == RelationshipDirection.OUTGOING ? DESTINATION : SOURCE;
    if (destinationTypes != null && destinationTypes.size() > 0) {
      finalQuery.must(QueryBuilders.termsQuery(destinationNode + ".entityType", destinationTypes));
    }
    addFilterToQueryBuilder(destinationEntityFilter, destinationNode, finalQuery);

    // set relationship filter
    if (relationshipTypes.size() > 0) {
      BoolQueryBuilder relationshipQuery = QueryBuilders.boolQuery();
      relationshipTypes.forEach(
          relationshipType -> relationshipQuery.should(QueryBuilders.termQuery(RELATIONSHIP_TYPE, relationshipType)));
      finalQuery.must(relationshipQuery);
    }
    return finalQuery;
  }

  @WithSpan
  public LineageResponse getLineage(@Nonnull Urn entityUrn, @Nonnull LineageDirection direction,
      GraphFilters graphFilters, int offset, int count,
      int maxHops, @Nullable Long startTimeMillis, @Nullable Long endTimeMillis) {
    List<LineageRelationship> result = new ArrayList<>();
    long currentTime = System.currentTimeMillis();
    long remainingTime = TIMEOUT_SECS * 1000;
    long timeoutTime = currentTime + remainingTime;

    // Do a Level-order BFS
    Set<Urn> visitedEntities = ConcurrentHashMap.newKeySet();
    visitedEntities.add(entityUrn);
    UrnArrayArray existingPaths = new UrnArrayArray();
    List<Urn> currentLevel = ImmutableList.of(entityUrn);

    for (int i = 0; i < maxHops; i++) {
      if (currentLevel.isEmpty()) {
        break;
      }

      if (remainingTime < 0) {
        log.info("Timed out while fetching lineage for {} with direction {}, maxHops {}. Returning results so far",
            entityUrn, direction, maxHops);
        break;
      }

      // Do one hop on the lineage graph
      List<LineageRelationship> oneHopRelationships =
          getLineageRelationshipsInBatches(
              currentLevel,
              direction,
              graphFilters,
              visitedEntities,
              i + 1,
              remainingTime,
              existingPaths,
              startTimeMillis,
              endTimeMillis);
      result.addAll(oneHopRelationships);
      currentLevel = oneHopRelationships.stream().map(LineageRelationship::getEntity).collect(Collectors.toList());
      currentTime = System.currentTimeMillis();
      remainingTime = timeoutTime - currentTime;
    }
    LineageResponse response = new LineageResponse(result.size(), result);

    List<LineageRelationship> subList;
    if (offset >= response.getTotal()) {
      subList = Collections.emptyList();
    } else {
      subList = response.getLineageRelationships().subList(offset, Math.min(offset + count, response.getTotal()));
    }

    return new LineageResponse(response.getTotal(), subList);
  }

  // Get 1-hop lineage relationships asynchronously in batches with timeout
  @WithSpan
  public List<LineageRelationship> getLineageRelationshipsInBatches(@Nonnull List<Urn> entityUrns,
      @Nonnull LineageDirection direction, GraphFilters graphFilters, Set<Urn> visitedEntities, int numHops,
      long remainingTime, UrnArrayArray existingPaths, @Nullable Long startTimeMillis,
      @Nullable Long endTimeMillis) {
    List<List<Urn>> batches = Lists.partition(entityUrns, BATCH_SIZE);
    return ConcurrencyUtils.getAllCompleted(batches.stream()
            .map(batchUrns -> CompletableFuture.supplyAsync(
                () -> getLineageRelationships(
                    batchUrns,
                    direction,
                    graphFilters,
                    visitedEntities,
                    numHops,
                    existingPaths,
                    startTimeMillis,
                    endTimeMillis)))
            .collect(Collectors.toList()), remainingTime, TimeUnit.MILLISECONDS)
        .stream()
        .flatMap(List::stream)
        .collect(Collectors.toList());
  }

  // Get 1-hop lineage relationships
  @WithSpan
  private List<LineageRelationship> getLineageRelationships(@Nonnull List<Urn> entityUrns,
      @Nonnull LineageDirection direction, GraphFilters graphFilters, Set<Urn> visitedEntities, int numHops,
      UrnArrayArray existingPaths, @Nullable Long startTimeMillis,
      @Nullable Long endTimeMillis) {
    Map<String, List<Urn>> urnsPerEntityType = entityUrns.stream().collect(Collectors.groupingBy(Urn::getEntityType));
    Map<String, List<EdgeInfo>> edgesPerEntityType = urnsPerEntityType.keySet()
        .stream()
        .collect(Collectors.toMap(Function.identity(),
            entityType -> lineageRegistry.getLineageRelationships(entityType, direction)));
    BoolQueryBuilder finalQuery = QueryBuilders.boolQuery();
    // Get all relation types relevant to the set of urns to hop from
    urnsPerEntityType.forEach((entityType, urns) -> finalQuery.should(
        getQueryForLineage(
            urns,
            edgesPerEntityType.getOrDefault(entityType, Collections.emptyList()),
            graphFilters,
            startTimeMillis,
            endTimeMillis)));
    SearchResponse response = executeSearchQuery(finalQuery, 0, MAX_ELASTIC_RESULT);
    Set<Urn> entityUrnSet = new HashSet<>(entityUrns);
    // Get all valid edges given the set of urns to hop from
    Set<Pair<String, EdgeInfo>> validEdges = edgesPerEntityType.entrySet()
        .stream()
        .flatMap(entry -> entry.getValue().stream().map(edgeInfo -> Pair.of(entry.getKey(), edgeInfo)))
        .collect(Collectors.toSet());
    return extractRelationships(entityUrnSet, response, validEdges, visitedEntities, numHops, existingPaths);
  }

  private UrnArrayArray getAndUpdatePaths(UrnArrayArray existingPaths, Urn parentUrn, Urn childUrn, RelationshipDirection direction) {
    try {
      UrnArrayArray currentPaths = existingPaths.stream()
          .filter(path -> path.get(direction == RelationshipDirection.OUTGOING ? 0 : path.size() - 1).equals(parentUrn))
          .collect(Collectors.toCollection(UrnArrayArray::new));
      UrnArrayArray resultPaths = new UrnArrayArray();
      if (currentPaths.size() > 0) {
        for (UrnArray path : currentPaths) {
          UrnArray copyOfPath = path.clone();
          if (direction == RelationshipDirection.OUTGOING) {
            copyOfPath.add(0, childUrn);
          } else {
            copyOfPath.add(childUrn);
          }
          resultPaths.add(copyOfPath);
          existingPaths.add(copyOfPath);
        }
      } else {
        UrnArray path = new UrnArray();
        if (direction == RelationshipDirection.OUTGOING) {
          path.addAll(ImmutableList.of(childUrn, parentUrn));
        } else {
          path.addAll(ImmutableList.of(parentUrn, childUrn));
        }
        resultPaths.add(path);
        existingPaths.add(path);
      }
      return resultPaths;
    } catch (CloneNotSupportedException e) {
      log.error(String.format("Failed to create paths for parentUrn %s and childUrn %s", parentUrn, childUrn), e);
      throw new RuntimeException(e);
    }
  }

  // Given set of edges and the search response, extract all valid edges that originate from the input entityUrns
  @WithSpan
  private List<LineageRelationship> extractRelationships(@Nonnull Set<Urn> entityUrns,
      @Nonnull SearchResponse searchResponse, Set<Pair<String, EdgeInfo>> validEdges, Set<Urn> visitedEntities,
      int numHops, UrnArrayArray existingPaths) {
    final List<LineageRelationship> result = new LinkedList<>();
    for (SearchHit hit : searchResponse.getHits().getHits()) {
      final Map<String, Object> document = hit.getSourceAsMap();
      final Urn sourceUrn = UrnUtils.getUrn(((Map<String, Object>) document.get(SOURCE)).get("urn").toString());
      final Urn destinationUrn =
          UrnUtils.getUrn(((Map<String, Object>) document.get(DESTINATION)).get("urn").toString());
      final String type = document.get(RELATIONSHIP_TYPE).toString();
      final Number createdOnNumber = (Number) document.getOrDefault(CREATED_ON, null);
      final Long createdOn = createdOnNumber != null ? createdOnNumber.longValue() : null;
      final Number updatedOnNumber = (Number) document.getOrDefault(UPDATED_ON, null);
      final Long updatedOn = updatedOnNumber != null ? updatedOnNumber.longValue() : null;
      final String createdActorString = (String) document.getOrDefault(CREATED_ACTOR, null);
      final Urn createdActor = createdActorString == null ? null : UrnUtils.getUrn(createdActorString);
      final String updatedActorString = (String) document.getOrDefault(UPDATED_ACTOR, null);
      final Urn updatedActor = updatedActorString == null ? null : UrnUtils.getUrn(updatedActorString);
      final Map<String, Object> properties;
      if (document.containsKey(PROPERTIES) && document.get(PROPERTIES) instanceof Map) {
        properties = (Map<String, Object>) document.get(PROPERTIES);
      } else {
        properties = Collections.emptyMap();
      }
      boolean isManual = properties.containsKey(SOURCE) && properties.get(SOURCE).equals("UI");

      // Potential outgoing edge
      if (entityUrns.contains(sourceUrn)) {
        // Skip if already visited
        // Skip if edge is not a valid outgoing edge
        if (!visitedEntities.contains(destinationUrn) && validEdges.contains(
            Pair.of(sourceUrn.getEntityType(),
                new EdgeInfo(type, RelationshipDirection.OUTGOING, destinationUrn.getEntityType().toLowerCase())))) {
          visitedEntities.add(destinationUrn);
          final UrnArrayArray paths =
              getAndUpdatePaths(existingPaths, sourceUrn, destinationUrn, RelationshipDirection.OUTGOING);
          final LineageRelationship relationship =
              createLineageRelationship(
                  type,
                  destinationUrn,
                  numHops,
                  paths,
                  createdOn,
                  createdActor,
                  updatedOn,
                  updatedActor,
                  isManual);
          result.add(relationship);
        }
      }

      // Potential incoming edge
      if (entityUrns.contains(destinationUrn)) {
        // Skip if already visited
        // Skip if edge is not a valid outgoing edge
        if (!visitedEntities.contains(sourceUrn) && validEdges.contains(
            Pair.of(destinationUrn.getEntityType(), new EdgeInfo(type, RelationshipDirection.INCOMING, sourceUrn.getEntityType().toLowerCase())))) {
          visitedEntities.add(sourceUrn);
          final UrnArrayArray paths =
              getAndUpdatePaths(existingPaths, destinationUrn, sourceUrn, RelationshipDirection.INCOMING);
          final LineageRelationship relationship = createLineageRelationship(
              type,
              sourceUrn,
              numHops,
              paths,
              createdOn,
              createdActor,
              updatedOn,
              updatedActor,
              isManual);
          result.add(relationship);
        }
      }
    }
    return result;
  }

  private LineageRelationship createLineageRelationship(
      @Nonnull final String type,
      @Nonnull final Urn entityUrn,
      final int numHops,
      @Nonnull final UrnArrayArray paths,
      @Nullable final Long createdOn,
      @Nullable final Urn createdActor,
      @Nullable final Long updatedOn,
      @Nullable final Urn updatedActor,
      final boolean isManual
  ) {
    final LineageRelationship relationship =
        new LineageRelationship().setType(type).setEntity(entityUrn).setDegree(numHops).setPaths(paths);
    if (createdOn != null) {
      relationship.setCreatedOn(createdOn);
    }
    if (createdActor != null) {
      relationship.setCreatedActor(createdActor);
    }
    if (updatedOn != null) {
      relationship.setUpdatedOn(updatedOn);
    }
    if (updatedActor != null) {
      relationship.setUpdatedActor(updatedActor);
    }
    relationship.setIsManual(isManual);
    return relationship;
  }

  BoolQueryBuilder getOutGoingEdgeQuery(List<Urn> urns, List<EdgeInfo> outgoingEdges, GraphFilters graphFilters) {
    BoolQueryBuilder outgoingEdgeQuery = QueryBuilders.boolQuery();
    outgoingEdgeQuery.must(buildUrnFilters(urns, SOURCE));
    outgoingEdgeQuery.must(buildEdgeFilters(outgoingEdges));
    outgoingEdgeQuery.must(buildEntityTypesFilter(graphFilters.getAllowedEntityTypes(), SOURCE));
    outgoingEdgeQuery.must(buildEntityTypesFilter(graphFilters.getAllowedEntityTypes(), DESTINATION));
    return outgoingEdgeQuery;
  }

  BoolQueryBuilder getIncomingEdgeQuery(List<Urn> urns, List<EdgeInfo> incomingEdges, GraphFilters graphFilters) {
    BoolQueryBuilder incomingEdgeQuery = QueryBuilders.boolQuery();
    incomingEdgeQuery.must(buildUrnFilters(urns, DESTINATION));
    incomingEdgeQuery.must(buildEdgeFilters(incomingEdges));
    incomingEdgeQuery.must(buildEntityTypesFilter(graphFilters.getAllowedEntityTypes(), SOURCE));
    incomingEdgeQuery.must(buildEntityTypesFilter(graphFilters.getAllowedEntityTypes(), DESTINATION));
    return incomingEdgeQuery;
  }

  // Get search query for given list of edges and source urns
  public QueryBuilder getQueryForLineage(List<Urn> urns, List<EdgeInfo> lineageEdges, GraphFilters graphFilters,
      @Nullable Long startTimeMillis,
      @Nullable Long endTimeMillis) {
    BoolQueryBuilder query = QueryBuilders.boolQuery();
    if (lineageEdges.isEmpty()) {
      return query;
    }
    Map<RelationshipDirection, List<EdgeInfo>> edgesByDirection =
        lineageEdges.stream().collect(Collectors.groupingBy(EdgeInfo::getDirection));

    List<EdgeInfo> outgoingEdges =
        edgesByDirection.getOrDefault(RelationshipDirection.OUTGOING, Collections.emptyList());
    if (!outgoingEdges.isEmpty()) {
      query.should(getOutGoingEdgeQuery(urns, outgoingEdges, graphFilters));
    }

    List<EdgeInfo> incomingEdges =
        edgesByDirection.getOrDefault(RelationshipDirection.INCOMING, Collections.emptyList());
    if (!incomingEdges.isEmpty()) {
      query.should(getIncomingEdgeQuery(urns, incomingEdges, graphFilters));
    }

    // Add time range filters
    if (startTimeMillis != null) {
      query.must(buildStartTimeFilter(startTimeMillis));
    }
    if (endTimeMillis != null) {
      query.must(buildEndTimeFilter(endTimeMillis));
    }

    return query;
  }

  public QueryBuilder buildEntityTypesFilter(List<String> entityTypes, String prefix) {
    return QueryBuilders.termsQuery(prefix + ".entityType", entityTypes.stream().map(Object::toString).collect(Collectors.toList()));
  }

  public QueryBuilder buildUrnFilters(List<Urn> urns, String prefix) {
    return QueryBuilders.termsQuery(prefix + ".urn", urns.stream().map(Object::toString).collect(Collectors.toList()));
  }

  public QueryBuilder buildEdgeFilters(List<EdgeInfo> edgeInfos) {
    return QueryBuilders.termsQuery("relationshipType",
        edgeInfos.stream().map(EdgeInfo::getType).distinct().collect(Collectors.toList()));
  }

  public QueryBuilder buildExistenceFilter() {
    final BoolQueryBuilder boolExistenceBuilder = QueryBuilders.boolQuery();
    boolExistenceBuilder.mustNot(QueryBuilders.existsQuery(CREATED_ON));
    boolExistenceBuilder.mustNot(QueryBuilders.existsQuery(UPDATED_ON));
    return boolExistenceBuilder;
  }

  public QueryBuilder buildManualLineageFilter() {
    return QueryBuilders.termQuery(String.format("%s.%s", PROPERTIES, SOURCE), UI);
  }

  public QueryBuilder buildStartTimeFilter(@Nonnull final Long startTimeMillis) {
    final BoolQueryBuilder startTimeQuery = QueryBuilders.boolQuery();
    startTimeQuery.should(QueryBuilders.rangeQuery(UPDATED_ON).gte(startTimeMillis));
    // Secondary check in case we only have createdOn
    startTimeQuery.should(QueryBuilders.rangeQuery(CREATED_ON).gte(startTimeMillis));
    // If both createdOn and updatedOn are not present, then we should include the edge
    startTimeQuery.should(buildExistenceFilter());
    // If the edge is a manual lineage edge, then we should include the edge
    startTimeQuery.should(buildManualLineageFilter());
    return startTimeQuery;
  }

  public QueryBuilder buildEndTimeFilter(@Nonnull final Long endTimeMillis) {
    final BoolQueryBuilder endTimeQuery = QueryBuilders.boolQuery();
    endTimeQuery.should(QueryBuilders.rangeQuery(CREATED_ON).lte(endTimeMillis));
    // If both createdOn and updatedOn are not present, then we should include the edge
    endTimeQuery.should(buildExistenceFilter());
    // If the edge is a manual lineage edge, then we should include the edge
    endTimeQuery.should(buildManualLineageFilter());
    return endTimeQuery;
  }

  @Value
  public static class LineageResponse {
    int total;
    List<LineageRelationship> lineageRelationships;
  }
}
